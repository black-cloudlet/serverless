"""The bounded executor and the admission gate every SSE stream passes through.

The problem this exists for: the Kubernetes client is synchronous, so a followed
pod log is a thread blocked on a socket for as long as the client stays
connected - not for the length of a request. Left on the default executor that
``asyncio.to_thread`` uses, a handful of idle log tails would occupy the same
threads every ordinary create, read and delete needs, and the API would stop
answering while looking completely healthy.

So streaming gets a pool of its own, sized from the admission bounds
(:class:`~api.core.config.StreamConfig`) rather than from a guess, and a gate
that refuses the stream that would overrun it. Refusing is the point: a 503 with
a retry is a client's problem to handle, while an accepted stream starved of a
thread looks to that client like a workload producing no logs.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import threading
from concurrent.futures import Executor, ThreadPoolExecutor

from cloudlet_apis.errors import ServiceUnavailableError
from cloudlet_apis.logging import get_logger

from api.core.config import StreamConfig

logger = get_logger(__name__)


async def run_on(executor: Executor | None, fn, *args):
    """Run blocking ``fn`` on ``executor``, preserving the request context.

    The context copy is what :func:`asyncio.to_thread` does and a bare
    ``run_in_executor`` does not: without it the correlation id that the log
    filter reads is missing from every line a stream's worker thread writes,
    which is exactly the debugging trail a long-lived stream most needs.

    Args:
        executor: The pool to run on, or None for the default one.
        fn: The blocking callable.
        *args: Positional arguments for ``fn``.

    Returns:
        Whatever ``fn`` returns.
    """
    return await start_on(executor, fn, *args)


def start_on(executor: Executor | None, fn, *args) -> asyncio.Future:
    """Start blocking ``fn`` on ``executor`` without awaiting it.

    The un-awaited form of :func:`run_on`, for a worker that outlives the call
    that started it (a log follower). Same context copy, same reason.

    Args:
        executor: The pool to run on, or None for the default one.
        fn: The blocking callable.
        *args: Positional arguments for ``fn``.

    Returns:
        The future the executor is running ``fn`` under.
    """
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    return loop.run_in_executor(executor, functools.partial(ctx.run, fn, *args))


class StreamSlot:
    """One admitted stream, released exactly once no matter how many paths try.

    Idempotent by design: teardown runs from whichever of several owners fires
    first (the generator's ``finally``, the acceptor's error path, the GC
    backstop for a generator that was never started), and a double release must
    not decrement the count below the true number of open streams.
    """

    def __init__(self, capacity: StreamCapacity):
        self._capacity = capacity
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        """Give the slot back. Safe to call more than once; only the first counts.

        Locked because the GC backstop may run off the event loop thread; the
        lock is what turns "several owners may release" into "exactly one does".
        """
        with self._lock:
            if self._released:
                return
            self._released = True
        self._capacity._release()


class StreamCapacity:
    """The stream thread pool, and the count of streams allowed to use it.

    One per process, built by the DI layer and shut down with the app.

    Attributes:
        config: The bounds this was built from.
    """

    def __init__(self, config: StreamConfig):
        """Build the pool at its derived size.

        Args:
            config: The stream bounds; ``max_workers`` sizes the pool and
                ``max_concurrent`` caps admissions.
        """
        self.config = config
        self._open = 0
        self._count_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=config.max_workers, thread_name_prefix="stream"
        )

    @property
    def executor(self) -> Executor:
        """The pool every stream's blocking work runs on."""
        return self._executor

    @property
    def open_streams(self) -> int:
        """How many streams are currently admitted."""
        return self._open

    async def run(self, fn, *args):
        """Run one blocking call on the stream pool (see :func:`run_on`)."""
        return await run_on(self._executor, fn, *args)

    def interval(self, requested: float | None) -> float:
        """Resolve a client's requested interval against the configured bounds.

        Clamped rather than rejected. The bounds exist to stop one client asking
        for a re-read every 50ms or going quiet for an hour, and neither is worth
        a 400 when the honest answer - "as often as this deployment allows" - is
        what the caller wanted. The floor is what protects the cluster; the
        ceiling is what keeps the connection from looking dead.

        Args:
            requested: The client's ``interval``, or None for the default.

        Returns:
            The interval to use, in seconds.
        """
        if requested is None:
            return self.config.interval_seconds
        return min(
            max(requested, self.config.min_interval_seconds), self.config.max_interval_seconds
        )

    def admit(self) -> StreamSlot:
        """Admit one stream, or refuse it.

        Not a queue: waiting for a slot would be the wrong behaviour - the
        client is holding a connection open expecting data, so being told to
        come back is strictly better than being connected and silent. The count
        is locked only because release has a GC backstop that may run off the
        event loop thread; admission itself always happens on it.

        Returns:
            The slot; the caller (or whoever it hands the slot to) must
            :meth:`~StreamSlot.release` it when the stream ends.

        Raises:
            ServiceUnavailableError: If ``max_concurrent`` streams are already open.
        """
        with self._count_lock:
            if self._open >= self.config.max_concurrent:
                open_now = self._open
            else:
                self._open += 1
                return StreamSlot(self)
        logger.warning(
            "refusing stream: %d of %d already open", open_now, self.config.max_concurrent
        )
        raise ServiceUnavailableError(
            f"too many open streams ({self.config.max_concurrent}); retry shortly, "
            "or poll the non-streaming endpoint"
        )

    def _release(self) -> None:
        """Return one admission (only ever via :meth:`StreamSlot.release`)."""
        with self._count_lock:
            self._open -= 1

    def shutdown(self) -> None:
        """Stop the pool, without waiting for threads still blocked on a read.

        ``cancel_futures`` clears what never started; the ones already reading
        are unblocked by their stream being closed as each generator tears down.
        Waiting here would hold shutdown open for as long as the slowest peer.
        """
        self._executor.shutdown(wait=False, cancel_futures=True)
