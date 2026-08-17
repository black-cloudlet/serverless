"""Following one pod's log, and turning that into a stream of events.

The stream is per **pod**, not per workload: the client picks a pod off the
roster (:mod:`api.services.streams.pods`) and opens a stream for it. That keeps
this module small - one pod, one thread, no set to reconcile - and moves the
choice of what to watch to the side that knows what the user is looking at.

Two moving parts remain, because the read is blocking and endless:

* :func:`_read` - the follow itself. Runs on a thread from the stream pool; the
  loop ends it by closing the underlying stream, which is the only thing that
  interrupts a blocking read (a flag is checked between lines, and a quiet pod
  produces none).
* :class:`_Buffer` - the hand-off. Bounded, because a pod logging faster than
  its reader must cost a *reported* gap rather than the process's memory.

Log lines cross the buffer as **rendered SSE frames** (str), everything else as
:class:`~api.services.streams.sse.StreamEvent`. The line path is the exception
deliberately: a pod can log tens of thousands of lines a second, and rendering
each on the event loop - the model dump, the frame, one generator hop per line -
starved the loop until the health probes missed and the kubelet restarted the
pod (killing every stream, whose clients then reconnected onto the surviving
replica and took it down the same way). The follower thread is already doing
per-line work, so it renders too, and the loop only forwards bytes - one yield
per buffer drain, not per line.
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
from common.cluster import Cluster

# `timestamps=True` prefixes every line with an RFC3339Nano stamp. Matched rather
# than split on whitespace so a workload logging something that merely looks like
# a date cannot have its first word eaten.
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

    Rendered in Israel local time, like every other timestamp the API returns
    (``createdAt``), so a client formats one timezone rather than two.

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

    ``len`` counts characters, and the budget this feeds is a memory bound: a
    workload logging anything outside ASCII (Hebrew, CJK, an emoji) spends up to
    four bytes per character, so counting characters would let the buffer hold
    several times the configured budget. ``isascii`` is a flag check on CPython,
    so the ordinary all-ASCII frame pays nothing for the distinction.

    Args:
        frame: The rendered SSE frame.

    Returns:
        Its length in UTF-8 bytes.
    """
    return len(frame) if frame.isascii() else len(frame.encode("utf-8"))


class _Buffer:
    """Bounded hand-off from the follower thread to the event loop.

    A plain :class:`asyncio.Queue` would not do: filling one from a thread means
    a ``call_soon_threadsafe`` per line, and scheduling those is itself unbounded
    - a pod that outruns its reader would then grow the loop's callback queue
    instead of the buffer, which is the same leak one level down. Here the thread
    appends under a lock and only wakes the loop on the empty-to-filled edge, so a
    burst of a thousand lines costs one wakeup and drops what does not fit.

    Items are rendered SSE frames (str) for log lines and
    :class:`~api.services.streams.sse.StreamEvent` for the rare control message;
    the buffer itself never looks inside them (see the module docstring for why
    the hot path is pre-rendered).

    Bounded twice, and both bounds matter. The line count caps the ordinary
    case; the byte budget caps the pathological one, where a pod writes without
    newlines and every "line" arrives as a ~1MB piece (LogFollow.MAX_LINE_BYTES)
    - a thousand of those is a gigabyte, which against the pod's memory limit is
    an OOM kill wearing a log stream's clothes. Either bound overrun costs a
    *reported* drop, exactly as the count bound always did.

    The byte budget is a bound on the buffer, not a target: a single frame too
    big to fit in an empty buffer is dropped rather than admitted. One frame is
    *not* bounded by MAX_LINE_BYTES - that bounds the raw line, and JSON
    escaping expands it (a megabyte of control bytes renders as six), so
    admitting one unconditionally would leave the pathological case unbounded
    in exactly the way this exists to prevent.
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

    The two halves are touched from different threads, which is the whole reason
    this is an object. The worker owns the iteration; the loop owns :meth:`stop`.
    They meet at the lock, so a stop that lands before the stream is even open
    still takes effect - otherwise a client that disconnects during the opening
    round trip would leave a thread following a pod nobody is reading.
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
    cluster: Cluster,
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
        follow.close()  # stopped while we were opening it
        tail.ended.set()
        return
    try:
        for line in follow.lines():
            stamp, message = split_timestamp(line)
            # Rendered here, on this thread, not on the event loop - the whole
            # point of the pre-rendered hot path (module docstring).
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

    The wire is identical - SSE frames concatenate - but the loop pays one yield
    (and the transport one write) per drain instead of per line, which under a
    fire-hosing pod is the difference between a busy loop and a starved one.

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
    cluster: Cluster,
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
        since_seconds: How far back the log starts, so a client sees recent
            context rather than only what arrives after it connected.
        tail_lines: Start at the newest this-many lines instead, however old
            they are - the right opening for a pod that has been quiet longer
            than any time window.

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
    # disconnects the instant it connects - a browser navigating away, a curl
    # piped into `head` - closes the generator at its very first suspension
    # point, and a teardown that only guarded the main loop would never run for
    # it. Those are exactly the streams that leak threads.
    # `start_on`, not a bare run_in_executor: the follower is the longest-lived
    # worker in the system, and its log lines need the request's correlation id.
    tail.future = start_on(
        capacity.executor, _read, cluster, tail, opening, since_seconds, tail_lines, buf
    )
    try:
        yield StreamEvent("open", opening)

        while True:
            now = loop.time()
            if now >= deadline:
                # Deliver what is already buffered before ending: the rollover
                # must not cost the client lines that had in fact arrived.
                for event in _coalesced(buf.drain()):
                    yield event
                # ...and report what was NOT delivered, exactly as a normal tick
                # would: a client reconnecting across the rollover must not read
                # the log as gapless when lines were in fact skipped.
                dropped = buf.take_dropped()
                if dropped:
                    yield StreamEvent(
                        "warning",
                        StreamWarning(
                            message=(
                                "the client is reading slower than the pod is logging; "
                                "lines were skipped"
                            ),
                            droppedLines=dropped,
                        ),
                    )
                # Not an error: the client reconnects, which SSE does unprompted.
                yield StreamEvent(
                    "end", StreamEnd(reason="the stream reached its time limit; reconnect")
                )
                return
            await buf.wait(min(deadline, now + config.heartbeat_seconds) - now)

            sent = False
            for event in _coalesced(buf.drain()):
                sent = True
                yield event

            dropped = buf.take_dropped()
            if dropped:
                sent = True
                yield StreamEvent(
                    "warning",
                    StreamWarning(
                        message=(
                            "the client is reading slower than the pod is logging; "
                            "lines were skipped"
                        ),
                        droppedLines=dropped,
                    ),
                )

            # Checked after draining, so the last lines a dying pod wrote are
            # delivered before the stream is closed on its behalf. `empty`, not
            # `drain`: draining here would consume - and discard - lines that
            # arrived since the drain above, exactly the ones this exists to save.
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


async def _teardown(tail: _Tail) -> None:
    """End the follow and wait, briefly, for its thread to notice.

    The wait is what keeps the admission bound honest: the stream's slot is
    released as this returns, so leaving without it would let a new stream in
    while this one's thread still holds the pool. It is bounded anyway - a thread
    that ignores a closed socket is a leak worth logging, not one worth hanging
    the teardown on.
    """
    tail.stop()
    if tail.future is None:
        return
    _done, pending = await asyncio.wait([tail.future], timeout=_DRAIN_TIMEOUT)
    if pending:
        logger.warning("a log follower did not stop within %ss", _DRAIN_TIMEOUT)
