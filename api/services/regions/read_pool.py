"""The bounded executor every cluster read runs on, with admission.

The streams' :class:`~api.services.streams.capacity.StreamCapacity` for the
read fan-outs: a pool sized from config, and a 503 past the bound instead of
a silently growing queue. :class:`~api.services.regions.deployer.Deployer` is
the only production caller.
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
    work (that region's failure row) - the two must not be conflated.
    """


class ReadPool:
    """The bounded executor every cluster *read* runs on, with admission.

    The same medicine :class:`~api.services.streams.capacity.StreamCapacity`
    applies to streams, applied to the read fan-outs. Without it every read
    rents a thread from the process-wide default executor - sized by a formula,
    shared with everything, queue unbounded - so a burst of page reads (a
    console tab polling row stats) makes *unrelated* requests inherit its
    latency invisibly. Here the pool is sized from config and admission past
    ``workers + max_queued`` is refused with 503: shed load is visible, a
    silently growing queue is not.

    Admission is counted on the event loop (every caller is a coroutine), so
    the counter needs no lock. What it counts is *thread occupancy*, not
    awaits: a read the caller's ``wait_for`` gave up on is still running on its
    worker - the executor cannot interrupt a thread - so the slot is released
    from the future's done callback, which fires when the thread actually
    finishes. Released on cancellation instead, a stalling region would fill
    the pool with zombie reads while the accounting reported it empty, and the
    503 shedding this class exists for would never fire.

    Admission is taken for a whole fan-out at once (:meth:`reserve`): refused
    part-way through, a fan-out would leave the regions it had already started
    burning the pool for results the 503 throws away.
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
        # Submitted directly, and the release hangs on the CONCURRENT future:
        # run_in_executor's asyncio wrapper acknowledges a cancel immediately
        # even while the thread runs on, so a callback there (or a finally on
        # the await) would free the slot the moment a caller's wait_for gave
        # up - long before the worker did. The concurrent future completes only
        # when the thread actually finishes (or was cancelled before starting),
        # which is the occupancy this counts. Context copied as start_on does,
        # so the read's log lines keep the request's correlation id.
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
