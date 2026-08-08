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

The events are :class:`~api.services.streams.sse.StreamEvent`; nothing here
knows it is being rendered as SSE.
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime

from cloudlet_apis.logging import get_logger

from api.core.config import StreamConfig
from api.models.common import LogLine, PodLogStreamOpen, StreamEnd, StreamWarning
from api.services.state.ksvc_state import ISRAEL_TZ
from api.services.streams.capacity import StreamCapacity
from api.services.streams.sse import StreamEvent, heartbeat
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


class _Buffer:
    """Bounded hand-off from the follower thread to the event loop.

    A plain :class:`asyncio.Queue` would not do: filling one from a thread means
    a ``call_soon_threadsafe`` per line, and scheduling those is itself unbounded
    - a pod that outruns its reader would then grow the loop's callback queue
    instead of the buffer, which is the same leak one level down. Here the thread
    appends under a lock and only wakes the loop on the empty-to-filled edge, so a
    burst of a thousand lines costs one wakeup and drops what does not fit.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, maxsize: int):
        """Initialize the buffer.

        Args:
            loop: The event loop to wake; captured because the producer is a
                thread, which has no running loop of its own.
            maxsize: Events held before new ones are dropped and counted.
        """
        self._loop = loop
        self._maxsize = maxsize
        self._items: deque = deque()
        self._dropped = 0
        self._lock = threading.Lock()
        self._ready = asyncio.Event()

    def put(self, item: StreamEvent) -> None:
        """Offer one event (called from the follower thread; never blocks)."""
        with self._lock:
            if len(self._items) >= self._maxsize:
                self._dropped += 1
                return
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

    def drain(self) -> list[StreamEvent]:
        """Take everything buffered (called on the event loop)."""
        with self._lock:
            items = list(self._items)
            self._items.clear()
            self._ready.clear()
        return items

    def take_dropped(self) -> int:
        """Take the drop count accumulated since the last call, and reset it."""
        with self._lock:
            dropped, self._dropped = self._dropped, 0
        return dropped

    async def wait(self, timeout: float) -> None:
        """Wait until something is buffered, or ``timeout`` elapses."""
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except TimeoutError, asyncio.TimeoutError:
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
    cluster: Cluster, tail: _Tail, opening: PodLogStreamOpen, since: int | None, buf: _Buffer
) -> None:
    """Follow the pod's log into the buffer (runs on a stream-pool thread).

    Never raises: this runs detached on the pool, so an exception here would be
    swallowed into a future nobody awaits. Whatever happens, ``tail.ended`` is
    set, which is how the loop learns the pod stopped producing and can close the
    stream instead of heartbeating at a client forever.
    """
    try:
        follow = cluster.follow_pod_logs(
            opening.pod, container=opening.container, since_seconds=since
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
            buf.put(
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
    except Exception:  # noqa: BLE001 - a closed stream lands here on teardown
        logger.debug("follow of pod '%s' ended", opening.pod, exc_info=True)
    finally:
        follow.close()
        tail.ended.set()


async def follow(
    *,
    cluster: Cluster,
    capacity: StreamCapacity,
    config: StreamConfig,
    opening: PodLogStreamOpen,
    since_seconds: int | None,
) -> AsyncIterator[StreamEvent]:
    """Stream one pod's log until it ends, the client leaves, or the cap passes.

    Args:
        cluster: The local site (logs are node-local; there is nowhere else to read).
        capacity: The stream pool the follower thread runs on.
        config: The stream bounds.
        opening: The ``open`` event, already built by the caller from the pod it
            authorized against.
        since_seconds: How far back the log starts, so a client sees recent
            context rather than only what arrives after it connected.

    Yields:
        The ``open`` event, then ``log``/``warning`` events and heartbeats, and a
        final ``end`` when the pod stops producing.
    """
    loop = asyncio.get_running_loop()
    buf = _Buffer(loop, config.queue_size)
    tail = _Tail()
    deadline = loop.time() + config.max_seconds

    # Started before the try, and the try covers the opening yield: a client that
    # disconnects the instant it connects - a browser navigating away, a curl
    # piped into `head` - closes the generator at its very first suspension
    # point, and a teardown that only guarded the main loop would never run for
    # it. Those are exactly the streams that leak threads.
    tail.future = loop.run_in_executor(
        capacity.executor, _read, cluster, tail, opening, since_seconds, buf
    )
    try:
        yield StreamEvent("open", opening)

        while True:
            now = loop.time()
            if now >= deadline:
                # Not an error: the client reconnects, which SSE does unprompted.
                yield StreamEvent(
                    "end", StreamEnd(reason="the stream reached its time limit; reconnect")
                )
                return
            await buf.wait(min(deadline, now + config.heartbeat_seconds) - now)

            sent = False
            for event in buf.drain():
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
            # delivered before the stream is closed on its behalf.
            if tail.ended.is_set() and not buf.drain():
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
