"""Following one pod's log, and turning that into a stream of events.

The stream is per **pod**, not per workload: the client picks a pod off the
roster (:mod:`api.services.streams.pods`) and opens a stream for it - one pod,
one thread, no set to reconcile (docs/ARCHITECTURE.md - Streaming).

Two moving parts carry it, because the read is blocking and endless:

* :func:`_read` - the follow itself. Runs on a thread from the stream pool; the
  loop ends it by closing the underlying stream, which is the only thing that
  interrupts a blocking read (a flag is checked between lines, and a quiet pod
  produces none).
* :class:`_Buffer` - the hand-off. Bounded: a pod logging faster than its reader
  costs a *reported* gap, not the process's memory.

Log lines cross the buffer as **rendered SSE frames** (str), everything else as
:class:`~api.services.streams.sse.StreamEvent`. The follower thread renders the
line path itself, so the event loop only forwards bytes - one yield per buffer
drain, not one per line.
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections import deque
from collections.abc import AsyncIterator, Iterator
from datetime import datetime

from cloudlet_apis.logging import get_logger

from api.core.config import StreamConfig
from api.models.common import LogLine, PodLogStreamOpen, StreamEnd, StreamWarning
from api.services.state.ksvc_state import ISRAEL_TZ
from api.services.streams.capacity import StreamCapacity, start_on
from api.services.streams.sse import StreamEvent, heartbeat, render
from common.cluster import NamespacedCluster

# `timestamps=True` prefixes every line with an RFC3339Nano stamp. The prefix is
# matched, not split off on whitespace, so a line that merely starts with
# something date-shaped keeps its first word.
_TIMESTAMP = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2}) (.*)$",
    re.DOTALL,
)

# How long teardown waits for the follower thread to notice its stream closed.
# Bounded because the slot is only given back afterwards: a thread wedged in a
# socket read must not hold a stream's admission open forever.
_DRAIN_TIMEOUT = 5.0

logger = get_logger(__name__)


def split_timestamp(line: str) -> tuple[datetime | None, str]:
    """Split the node's timestamp prefix off a log line.

    The time is returned in Israel local time, like every other timestamp the API
    returns (``createdAt``).

    Args:
        line: The raw line, as ``timestamps=True`` produced it.

    Returns:
        ``(time, message)``; ``time`` is None when the line carried no parseable
        stamp, in which case the whole line is the message.
    """
    match = _TIMESTAMP.match(line)
    if not match:
        return None, line
    base, fraction, offset, message = match.groups()
    # Kubernetes stamps nanoseconds; fromisoformat takes at most microseconds.
    micros = f".{fraction[:6]}" if fraction else ""
    try:
        stamp = datetime.fromisoformat(f"{base}{micros}{offset.replace('Z', '+00:00')}")
    except ValueError:
        return None, line
    return stamp.astimezone(ISRAEL_TZ), message


def _frame_bytes(frame: str) -> int:
    """How much memory a rendered frame costs, in bytes.

    ``len`` counts characters, and outside ASCII one character costs up to four
    bytes, so a character count would let the buffer hold several times its byte
    budget. ``isascii`` is a flag check on CPython.

    Args:
        frame: The rendered SSE frame.

    Returns:
        Its length in UTF-8 bytes.
    """
    return len(frame) if frame.isascii() else len(frame.encode("utf-8"))


class _Buffer:
    """Bounded hand-off from the follower thread to the event loop.

    The producer thread appends under a lock and wakes the loop only on the
    empty-to-filled edge, so a burst of a thousand lines costs one wakeup and
    drops whatever does not fit.

    Items are rendered SSE frames (str) for log lines and
    :class:`~api.services.streams.sse.StreamEvent` for the rare control message;
    the buffer itself never looks inside them.

    Bounded twice: ``maxsize`` caps how many items are held, ``max_bytes`` how
    many rendered bytes. A pod that writes without newlines reaches the byte
    bound first, each "line" arriving as up to ``LogFollow.MAX_LINE_BYTES``
    (~1MB). Overrunning either bound costs a *reported* drop.

    A frame too big for even an empty buffer is dropped: ``MAX_LINE_BYTES``
    bounds the raw line, not the frame JSON escaping makes of it.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, maxsize: int, max_bytes: int):
        """Initialize the buffer.

        Args:
            loop: The event loop to wake; captured because the producer is a
                thread, which has no running loop of its own.
            maxsize: Events held before new ones are dropped and counted.
            max_bytes: Frame bytes held before new frames are dropped and
                counted, whatever the count bound still allows.
        """
        self._loop = loop
        self._maxsize = maxsize
        self._max_bytes = max_bytes
        self._bytes = 0
        self._items: deque = deque()
        self._dropped = 0
        self._lock = threading.Lock()
        self._ready = asyncio.Event()

    def put(self, item: StreamEvent | str) -> None:
        """Offer one event or rendered frame (from the follower thread; never blocks)."""
        size = _frame_bytes(item) if isinstance(item, str) else 0
        with self._lock:
            over_bytes = self._bytes + size > self._max_bytes
            if len(self._items) >= self._maxsize or over_bytes:
                self._dropped += 1
                return
            self._bytes += size
            self._items.append(item)
            # Only on the empty-to-filled edge. `drain` clears the flag under
            # this same lock and only ever leaves the deque empty, so a producer
            # that arrives after a drain always sees an empty deque and wakes -
            # the wakeup cannot be lost, only occasionally repeated.
            wake = len(self._items) == 1
        if wake:
            try:
                self._loop.call_soon_threadsafe(self._ready.set)
            except RuntimeError:  # noqa: S110 - loop already closed; teardown is under way
                pass

    def drain(self) -> list[StreamEvent | str]:
        """Take everything buffered (called on the event loop)."""
        with self._lock:
            items = list(self._items)
            self._items.clear()
            self._bytes = 0
            self._ready.clear()
        return items

    def empty(self) -> bool:
        """Whether nothing is buffered (without consuming anything)."""
        with self._lock:
            return not self._items

    def take_dropped(self) -> int:
        """Take the drop count accumulated since the last call, and reset it."""
        with self._lock:
            dropped, self._dropped = self._dropped, 0
        return dropped

    async def wait(self, timeout: float) -> None:
        """Wait until something is buffered, or ``timeout`` elapses."""
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except TimeoutError:
            pass


class _Tail:
    """The running follow, and the handle that ends it.

    The two halves are touched from different threads: the worker owns the
    iteration, the event loop owns :meth:`stop`. They meet at the lock, so a stop
    that lands before the stream is open still takes effect - :meth:`attach` then
    returns False and the opener closes what it just opened.
    """

    def __init__(self):
        """Initialize the tail (nothing is opened yet)."""
        self.future: asyncio.Future | None = None
        self.ended = threading.Event()
        self._follow = None
        self._stopped = False
        self._lock = threading.Lock()

    def attach(self, follow) -> bool:
        """Hand the opened stream over.

        Returns:
            False if :meth:`stop` already ran, meaning the caller should close
            what it just opened and give up.
        """
        with self._lock:
            if self._stopped:
                return False
            self._follow = follow
            return True

    def stop(self) -> None:
        """End the follow, unblocking its thread. Idempotent."""
        with self._lock:
            self._stopped = True
            follow, self._follow = self._follow, None
        if follow is not None:
            follow.close()


def _read(
    cluster: NamespacedCluster,
    tail: _Tail,
    opening: PodLogStreamOpen,
    since: int | None,
    tail_lines: int | None,
    buf: _Buffer,
) -> None:
    """Follow the pod's log into the buffer (runs on a stream-pool thread).

    Never raises: this runs detached on the pool, so an exception here would be
    swallowed into a future nobody awaits. Whatever happens, ``tail.ended`` is
    set, which is how the loop learns the pod stopped producing and can close the
    stream instead of heartbeating at a client forever.
    """
    try:
        follow = cluster.follow_pod_logs(
            opening.pod, container=opening.container, since_seconds=since, tail_lines=tail_lines
        )
    except Exception as exc:  # noqa: BLE001 - reported to the client, never raised
        logger.warning("could not follow pod '%s': %s", opening.pod, exc)
        buf.put(
            StreamEvent("warning", StreamWarning(message=f"could not read pod '{opening.pod}'"))
        )
        tail.ended.set()
        return
    if not tail.attach(follow):
        follow.close()  # stopped while it was being opened
        tail.ended.set()
        return
    try:
        for line in follow.lines():
            stamp, message = split_timestamp(line)
            # Rendered on this thread, not on the event loop (module docstring).
            buf.put(
                render(
                    StreamEvent(
                        "log",
                        LogLine(
                            pod=opening.pod,
                            container=opening.container,
                            revision=opening.revision,
                            time=stamp,
                            message=message,
                        ),
                    )
                )
            )
    except Exception:  # noqa: BLE001 - a closed stream lands here on teardown
        logger.debug("follow of pod '%s' ended", opening.pod, exc_info=True)
    finally:
        follow.close()
        tail.ended.set()


def _coalesced(items: list[StreamEvent | str]) -> Iterator[StreamEvent | str]:
    """Join each run of rendered frames into one, leaving control events alone.

    The wire is identical - SSE frames concatenate - and the loop pays one yield
    (and the transport one write) per drain instead of one per line.

    Args:
        items: One buffer drain: rendered frames (str) and control events.

    Yields:
        The items in order, with adjacent frames concatenated.
    """
    frames: list[str] = []
    for item in items:
        if isinstance(item, str):
            frames.append(item)
            continue
        if frames:
            yield "".join(frames)
            frames.clear()
        yield item
    if frames:
        yield "".join(frames)


async def follow(
    *,
    cluster: NamespacedCluster,
    capacity: StreamCapacity,
    config: StreamConfig,
    opening: PodLogStreamOpen,
    since_seconds: int | None,
    tail_lines: int | None = None,
) -> AsyncIterator[StreamEvent | str]:
    """Stream one pod's log until it ends, the client leaves, or the cap passes.

    Args:
        cluster: The local region (logs are node-local; there is nowhere else to read).
        capacity: The stream pool the follower thread runs on.
        config: The stream bounds.
        opening: The ``open`` event, already built by the caller from the pod it
            authorized against.
        since_seconds: How far back the log starts, so the client opens with
            recent context and not only what arrives after it connected.
        tail_lines: Start at the newest this-many lines instead, however old
            they are.

    Yields:
        The ``open`` event, then ``log`` frames (pre-rendered SSE, str - see the
        module docstring), ``warning`` events and heartbeats, and a final ``end``
        when the pod stops producing.
    """
    loop = asyncio.get_running_loop()
    buf = _Buffer(loop, config.queue_size, config.queue_max_bytes)
    tail = _Tail()
    deadline = loop.time() + config.max_seconds

    # Started before the try, and the try covers the opening yield: a client that
    # disconnects immediately closes the generator at its first suspension point,
    # and the finally below still tears the follower down. `start_on` copies the
    # request context, so the follower's log lines carry the correlation id.
    tail.future = start_on(
        capacity.executor, _read, cluster, tail, opening, since_seconds, tail_lines, buf
    )
    try:
        yield StreamEvent("open", opening)

        while True:
            now = loop.time()
            if now >= deadline:
                # Deliver what is already buffered before ending, and report
                # what was dropped, exactly as an ordinary tick does.
                for event in _flush(buf):
                    yield event
                # Not an error: the client reconnects, which SSE does unprompted.
                yield StreamEvent(
                    "end", StreamEnd(reason="the stream reached its time limit; reconnect")
                )
                return
            await buf.wait(min(deadline, now + config.heartbeat_seconds) - now)

            sent = False
            for event in _flush(buf):
                sent = True
                yield event

            # Checked after draining, so the last lines a dying pod wrote are
            # delivered before the stream is closed on its behalf. `empty`, not
            # `drain`: a drain here would consume and discard the lines that
            # arrived since the drain above.
            if tail.ended.is_set() and buf.empty():
                yield StreamEvent(
                    "end",
                    StreamEnd(
                        reason=(
                            f"the log of pod '{opening.pod}' ended; it stopped or was "
                            "replaced by a new revision"
                        )
                    ),
                )
                return

            if not sent:
                yield heartbeat()
    finally:
        await _teardown(tail)


def _flush(buf: _Buffer) -> Iterator[StreamEvent | str]:
    """Everything a tick delivers: the buffered lines, then what was dropped.

    The drop count is reported after the lines it was counted against, so a
    client that renders in order sees the gap where it happened.

    Args:
        buf: The follower's buffer.

    Yields:
        The coalesced line frames, then a ``warning`` if any were dropped.
    """
    yield from _coalesced(buf.drain())
    dropped = buf.take_dropped()
    if dropped:
        yield StreamEvent(
            "warning",
            StreamWarning(
                message="the client is reading slower than the pod is logging; lines were skipped",
                droppedLines=dropped,
            ),
        )


async def _teardown(tail: _Tail) -> None:
    """End the follow and wait, briefly, for its thread to notice.

    The stream's slot is released as this returns, so the wait keeps a finished
    stream's thread from overlapping a newly admitted one. It is bounded by
    ``_DRAIN_TIMEOUT``; a follower still running after that is logged and left.
    """
    tail.stop()
    if tail.future is None:
        return
    _done, pending = await asyncio.wait([tail.future], timeout=_DRAIN_TIMEOUT)
    if pending:
        logger.warning("a log follower did not stop within %ss", _DRAIN_TIMEOUT)
