"""The bounded executor every cluster read runs on, with admission.

A thread pool sized from config, with admission: a read past the bound is
refused with a 503 instead of joining an unbounded queue.
:class:`~api.services.regions.deployer.Deployer` is the only production caller.
"""

from __future__ import annotations

import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from cloudlet_apis.errors import ServiceUnavailableError
from cloudlet_apis.logging import get_logger

logger = get_logger(__name__)


class ReadPoolSaturated(ServiceUnavailableError):
    """The read pool refused an admission.

    Its own type so the fan-out can tell "the API is saturated" (this request's
    503) from a ``ServiceUnavailableError`` a region function raised doing its
    work (that region's failure row).
    """


class ReadPool:
    """The bounded executor every cluster *read* runs on, with admission.

    Reads run here rather than on the process-wide default executor: the pool is
    sized from config and admission past ``workers + max_queued`` reads in flight
    is refused with a 503.

    Admission is counted on the event loop (every caller is a coroutine), so
    the counter needs no lock. What it counts is *thread occupancy*, not
    awaits: a read the caller's ``wait_for`` gave up on is still running on its
    worker - the executor cannot interrupt a thread - so the slot is released
    from the future's done callback, which fires when the thread actually
    finishes.

    Admission for a whole fan-out is taken at once (:meth:`reserve`): the group
    is admitted or refused as one, never part-way through.
    """

    def __init__(self, workers: int, max_queued: int):
        """Build the pool.

        Args:
            workers: Threads reads run on.
            max_queued: Reads allowed to wait for a thread beyond ``workers``.
        """
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cluster-read")
        self._limit = workers + max_queued
        self._inflight = 0

    @contextmanager
    def reserve(self, count: int):
        """Admit ``count`` reads together, or refuse the group.

        Args:
            count: How many reads the caller is about to run.

        Yields:
            The coroutine function to run each read with. What it is not called
            for is given back on exit; what it started releases on its own, when
            the thread finishes.

        Raises:
            ServiceUnavailableError: If the group does not fit under
                ``workers + max_queued`` reads in flight.
        """
        if self._inflight + count > self._limit:
            logger.warning(
                "refusing %d cluster read(s): %d already in flight", count, self._inflight
            )
            raise ReadPoolSaturated("the API is saturated with cluster reads; retry shortly")
        self._inflight += count
        unspent = count

        async def run(fn, *args):
            nonlocal unspent
            unspent -= 1  # in the same synchronous step as the submit below
            return await self._run_reserved(fn, *args)

        try:
            yield run
        finally:
            self._release(unspent)

    async def run(self, fn, *args):
        """Reserve one admission and run one blocking read on the pool.

        Raises:
            ServiceUnavailableError: If the pool is saturated.
        """
        with self.reserve(1) as run_one:
            return await run_one(fn, *args)

    async def _run_reserved(self, fn, *args):
        """Run a read whose admission :meth:`reserve` already took."""
        loop = asyncio.get_running_loop()
        # Submitted directly, with the release hanging on the CONCURRENT future:
        # it completes when the thread finishes (or was cancelled before it
        # started), which is the occupancy this counts, while asyncio's
        # run_in_executor wrapper acknowledges a cancel while the thread runs on.
        # Context is copied as start_on does, so the read's log lines keep the
        # request's correlation id.
        ctx = contextvars.copy_context()
        try:
            concurrent_future = self._executor.submit(ctx.run, fn, *args)
        except RuntimeError:
            self._release()  # shut down under us: no thread, so no done callback
            raise

        def release(_cf) -> None:
            # Runs on the worker thread; the counter is only touched on the loop.
            try:
                loop.call_soon_threadsafe(self._release)
            except RuntimeError:  # noqa: S110 - loop closed; teardown is under way
                pass

        concurrent_future.add_done_callback(release)
        return await asyncio.wrap_future(concurrent_future)

    def _release(self, count: int = 1) -> None:
        """Give admissions back (on the loop, as each thread finishes)."""
        self._inflight -= count

    def shutdown(self) -> None:
        """Stop the pool without waiting: socket timeouts bound what's running."""
        self._executor.shutdown(wait=False, cancel_futures=True)
