"""Following a workload's pod logs, and turning that into a stream of events.

The shape of the problem: reading a pod's log is blocking and endless, the pods
being read change while the client is connected, and the client may read slower
than the workload writes. So there are three moving parts here.

* :class:`_Tail` - one pod's follow. Runs on a thread from the stream pool and
  hands lines over; the loop ends it by closing its stream, which is the only
  thing that interrupts a blocking read.
* :class:`_Buffer` - the hand-off. Bounded, because a workload logging faster
  than its reader must cost a *reported* gap rather than the process's memory.
* :func:`follow` - the loop. Drains the buffer, re-lists pods on an interval so
  a scale-up or a new revision is picked up, and tears everything down when the
  client goes away.

The events themselves are :class:`~api.services.streams.sse.StreamEvent`; nothing
here knows it is being rendered as SSE.
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime

from cloudlet_apis.errors import NotFoundError
from cloudlet_apis.logging import get_logger

from api.core.config import StreamConfig
from api.models.common import LogLine, LogStreamOpen, PodChange, StreamWarning
from api.services.state.ksvc_state import ISRAEL_TZ
from api.services.streams.capacity import StreamCapacity
from api.services.streams.sse import StreamEvent, heartbeat
from common.cluster import Cluster, ResourceKind

logger = get_logger(__name__)

REVISION_LABEL = "serving.knative.dev/revision"
SERVICE_LABEL = "serving.knative.dev/service"

# `timestamps=True` prefixes every line with an RFC3339Nano stamp. Matched rather
# than split on whitespace so a workload logging something that merely looks like
# a date cannot have its first word eaten.
_TIMESTAMP = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2}) (.*)$",
    re.DOTALL,
)

# How long teardown waits for the follower threads to notice their stream closed.
# Bounded because the slot is only given back afterwards: a thread wedged in a
# socket read must not hold a stream's admission open forever.
_DRAIN_TIMEOUT = 5.0


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
    """Bounded hand-off from the follower threads to the event loop.

    A plain :class:`asyncio.Queue` would not do: filling one from a thread means
    a ``call_soon_threadsafe`` per line, and scheduling those is itself unbounded
    - a workload that outruns its reader would then grow the loop's callback
    queue instead of the buffer, which is the same leak one level down. Here the
    threads append under a lock and only wake the loop on the empty-to-filled
    edge, so a burst of a thousand lines costs one wakeup and drops what does not
    fit.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, maxsize: int):
        """Initialize the buffer.

        Args:
            loop: The event loop to wake; captured because the producers are
                threads, which have no running loop of their own.
            maxsize: Events held before new ones are dropped and counted.
        """
        self._loop = loop
        self._maxsize = maxsize
        self._items: deque = deque()
        self._dropped = 0
        self._lock = threading.Lock()
        self._ready = asyncio.Event()

    def put(self, item: StreamEvent) -> None:
        """Offer one event (called from a follower thread; never blocks)."""
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
    """One pod's follow: the running work, and the handle that ends it.

    The two halves are touched from different threads, which is the whole reason
    this is an object. The worker owns the iteration; the loop owns
    :meth:`stop`. They meet at the lock, so a stop that lands before the stream
    is even open still takes effect - otherwise a pod that is removed in the
    same tick it was added would be followed forever.
    """

    def __init__(self, pod: str, revision: str | None):
        """Initialize the tail for one pod (nothing is opened yet).

        Args:
            pod: The pod name.
            revision: The Knative revision it belongs to, if labelled.
        """
        self.pod = pod
        self.revision = revision
        self.future: asyncio.Future | None = None
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


def _read(cluster: Cluster, tail: _Tail, container: str, since: int | None, buf: _Buffer) -> None:
    """Follow one pod's log into the buffer (runs on a stream-pool thread).

    Never raises: this runs detached on the pool, so an exception here would be
    swallowed into a future nobody awaits. A pod that cannot be read is reported
    as a warning event and the rest of the stream carries on - one unreadable
    pod out of five is not a reason to end the client's stream.
    """
    try:
        follow = cluster.follow_pod_logs(tail.pod, container=container, since_seconds=since)
    except NotFoundError:
        return  # raced a scale-down between the listing and the read
    except Exception as exc:  # noqa: BLE001 - reported to the client, never raised
        logger.warning("could not follow pod '%s': %s", tail.pod, exc)
        buf.put(
            StreamEvent(
                "warning",
                StreamWarning(message=f"could not follow pod '{tail.pod}'", pods=[tail.pod]),
            )
        )
        return
    if not tail.attach(follow):
        follow.close()  # stopped while we were opening it
        return
    try:
        for line in follow.lines():
            stamp, message = split_timestamp(line)
            buf.put(
                StreamEvent(
                    "log",
                    LogLine(
                        pod=tail.pod,
                        container=container,
                        revision=tail.revision,
                        time=stamp,
                        message=message,
                    ),
                )
            )
    except Exception:  # noqa: BLE001 - a closed stream lands here on teardown
        logger.debug("follow of pod '%s' ended", tail.pod, exc_info=True)
    finally:
        follow.close()


def list_pods(cluster: Cluster, oname: str) -> dict[str, str | None]:
    """The workload's pods on this site, mapped to their revision.

    Args:
        cluster: The site to list on.
        oname: The object name (``{name}-{group}``).

    Returns:
        ``{pod_name: revision_or_None}``; empty when scaled to zero.
    """
    pods = cluster.get(ResourceKind.POD, label_selector=f"{SERVICE_LABEL}={oname}")
    found: dict[str, str | None] = {}
    for pod in pods:
        meta = pod.get("metadata", {}) or {}
        name = meta.get("name")
        if name:
            found[name] = (meta.get("labels", {}) or {}).get(REVISION_LABEL)
    return found


async def follow(
    *,
    cluster: Cluster,
    capacity: StreamCapacity,
    config: StreamConfig,
    opening: LogStreamOpen,
    oname: str,
    pods: dict[str, str | None],
    since_seconds: int | None,
    interval: float,
) -> AsyncIterator[StreamEvent]:
    """Stream a workload's pod logs until the client leaves or the deadline passes.

    Args:
        cluster: The local site (logs are node-local; there is nowhere else to read).
        capacity: The stream pool the follower threads run on.
        config: The stream bounds.
        opening: The ``open`` event, already built by the caller from the state it
            authorized against.
        oname: The object name (``{name}-{group}``).
        pods: The pods found when the stream was authorized, so the first tails
            start without waiting a full interval.
        since_seconds: How far back each pod's log starts.
        interval: Seconds between pod re-listings.

    Yields:
        The ``open`` event, then ``log``/``pods``/``warning`` events and
        heartbeats until the stream ends.
    """
    loop = asyncio.get_running_loop()
    buf = _Buffer(loop, config.queue_size)
    tails: dict[str, _Tail] = {}
    deadline = loop.time() + config.max_seconds
    capped: list[str] = []

    def start(found: dict[str, str | None]) -> tuple[list[str], list[str]]:
        """Reconcile the running tails against ``found``; returns (added, removed)."""
        nonlocal capped
        removed = [pod for pod in tails if pod not in found]
        for pod in removed:
            tails.pop(pod).stop()
        added: list[str] = []
        over: list[str] = []
        for pod, revision in found.items():
            if pod in tails:
                continue
            if len(tails) >= config.max_pods:
                over.append(pod)
                continue
            tail = _Tail(pod, revision)
            tails[pod] = tail
            tail.future = loop.run_in_executor(
                capacity.executor, _read, cluster, tail, opening.container, since_seconds, buf
            )
            added.append(pod)
        capped = over
        return added, removed

    # Everything from the first tail onward is inside the try, including the
    # opening yields. A client that disconnects the instant it connects - a
    # browser navigating away, a curl piped into `head` - closes the generator at
    # its very first suspension point, and a teardown that only guarded the main
    # loop would never run for it. Those are exactly the streams that leak
    # threads, because they are the ones nothing else cleans up after.
    start(pods)
    try:
        yield StreamEvent("open", opening)
        if capped:
            yield StreamEvent(
                "warning",
                StreamWarning(
                    message=(
                        f"following {config.max_pods} of {len(pods)} pods "
                        f"(the per-stream limit); the rest are not being read"
                    ),
                    pods=sorted(capped),
                ),
            )

        next_refresh = loop.time() + interval
        while True:
            now = loop.time()
            if now >= deadline:
                return  # the client reconnects; SSE does that on its own
            await buf.wait(min(next_refresh, deadline, now + config.heartbeat_seconds) - now)

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
                            "the client is reading slower than the workload is logging; "
                            "lines were skipped"
                        ),
                        droppedLines=dropped,
                    ),
                )

            if loop.time() >= next_refresh:
                next_refresh = loop.time() + interval
                try:
                    found = await capacity.run(list_pods, cluster, oname)
                except Exception as exc:  # noqa: BLE001 - a failed re-list is not fatal
                    logger.warning("could not re-list pods for '%s': %s", oname, exc)
                    found = None
                if found is not None:
                    was_capped = list(capped)
                    added, removed = start(found)
                    if added or removed:
                        sent = True
                        yield StreamEvent(
                            "pods",
                            PodChange(
                                added=sorted(added),
                                removed=sorted(removed),
                                following=sorted(tails),
                            ),
                        )
                    if capped and capped != was_capped:
                        sent = True
                        yield StreamEvent(
                            "warning",
                            StreamWarning(
                                message=(
                                    f"following {config.max_pods} pods (the per-stream "
                                    "limit); the rest are not being read"
                                ),
                                pods=sorted(capped),
                            ),
                        )

            if not sent:
                yield heartbeat()
    finally:
        await _teardown(tails)


async def _teardown(tails: dict[str, _Tail]) -> None:
    """End every follow and wait, briefly, for its thread to notice.

    The wait is what keeps the admission bound honest: the stream's slot is
    released as this returns, so leaving without it would let a new stream in
    while this one's threads still hold the pool. It is bounded anyway - a thread
    that ignores a closed socket is a leak worth logging, not one worth hanging
    the teardown on.
    """
    for tail in tails.values():
        tail.stop()
    futures = [tail.future for tail in tails.values() if tail.future is not None]
    if not futures:
        return
    _done, pending = await asyncio.wait(futures, timeout=_DRAIN_TIMEOUT)
    if pending:
        logger.warning("%d log follower(s) did not stop within %ss", len(pending), _DRAIN_TIMEOUT)
