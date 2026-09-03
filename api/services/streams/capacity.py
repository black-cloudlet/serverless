"""The bounded executor and the admission gate every SSE stream passes through.

The Kubernetes client is synchronous, so a followed pod log is a thread blocked
on a socket for as long as the client stays connected - not for the length of a
request.

Streaming therefore runs on a pool of its own, sized from the admission bounds
(:class:`~api.core.config.StreamConfig`), behind a gate that refuses with a 503
any stream that would overrun it
(docs/STREAMING.md - A held-open stream holds a thread).
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

    The context is copied as :func:`asyncio.to_thread` does and a bare
    ``run_in_executor`` does not, so the correlation id the log filter reads
    reaches every line the worker thread writes.

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
    that started it (a log follower). Copies the context the same way.

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

    Teardown runs from whichever of several owners fires first (the generator's
    ``finally``, the acceptor's error path, the GC backstop for a generator that
    was never started); only the first release decrements the open-stream count.
    """

    def __init__(self, capacity: StreamCapacity):
        self._capacity = capacity
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        """Give the slot back. Safe to call more than once; only the first counts.

        Locked because the GC backstop may run off the event loop thread, so
        exactly one of several concurrent releases counts.
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

        An out-of-range interval is clamped to ``min_interval_seconds`` /
        ``max_interval_seconds`` rather than rejected; None takes
        ``interval_seconds``.

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

        Not a queue: a stream over ``max_concurrent`` is refused with a 503
        immediately, never made to wait for a slot. The count is locked only
        because release has a GC backstop that may run off the event loop
        thread; admission itself always happens on it.

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
        """
        self._executor.shutdown(wait=False, cancel_futures=True)
