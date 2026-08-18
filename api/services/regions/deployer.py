"""Multi-region fan-out (docs/ARCHITECTURE.md - Multi-Region).

Every deploy is applied to all target regions concurrently; the per-region
results are rolled up by :mod:`api.services.regions.rollup`. The Kubernetes
client is synchronous, so per-region work runs in threads.
"""

from __future__ import annotations

import asyncio
import contextvars
from concurrent.futures import Executor, ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from typing import Callable

from cloudlet_apis.errors import ServiceUnavailableError
from cloudlet_apis.logging import get_logger

from api.core.config import Settings
from api.models.common import RegionStatus
from api.services.streams.capacity import run_on
from common.cluster import Cluster, clusters_for, select_local
from common.errors import ValidationError

logger = get_logger(__name__)

# fn(cluster) -> RegionStatus  (may run blocking I/O; executed in a thread)
RegionFn = Callable[[Cluster], RegionStatus]


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


class Deployer:
    """Owns the per-region cluster clients and runs work across them concurrently."""

    def __init__(self, settings: Settings):
        """Build one cluster client per configured region (connections stay lazy).

        Args:
            settings: Global settings; only the per-region timeout, local region, and
                the built Clusters are retained.
        """
        self._op_timeout = settings.cluster_op_timeout
        self._read_timeout = settings.cluster_read_op_timeout
        self._local_region = settings.local_region
        self._clusters: dict[str, Cluster] = clusters_for(settings)
        self._read_pool = ReadPool(settings.cluster_read_workers, settings.cluster_read_max_queued)

    def close(self) -> None:
        """Release the read pool and every region's cluster client at shutdown."""
        self._read_pool.shutdown()
        for cluster in self._clusters.values():
            cluster.close()

    async def run_read(self, fn, *args):
        """Run one blocking cluster read on the bounded read pool.

        For the single-cluster reads that serve GETs, so they share the
        fan-out's pool and admission rather than the default executor. Bounded
        by ``cluster_read_op_timeout`` like every read: a slot is released when
        the *thread* finishes, so an unbounded caller holds its worker.

        Raises:
            ServiceUnavailableError: If the pool is saturated, or the read did
                not finish in time.
        """
        try:
            return await asyncio.wait_for(
                self._read_pool.run(fn, *args), timeout=self._read_timeout
            )
        except asyncio.TimeoutError as exc:
            logger.warning("cluster read timed out after %ss", self._read_timeout)
            raise ServiceUnavailableError(
                f"the cluster did not answer within {self._read_timeout}s; retry shortly"
            ) from exc

    def local_cluster(self) -> Cluster:
        """The cluster this API instance sits in.

        Selected by config ``local_region`` (matched by region name then cluster
        name), falling back to the first configured region. Used for reads of data
        that is uniform across regions (active/active), to avoid a cross-cluster
        round trip.

        Returns:
            The local cluster.

        Raises:
            ValidationError: If no regions are configured.
        """
        return select_local(self._clusters, self._local_region)

    def local_region(self) -> str:
        """The name of the local region (see :meth:`local_cluster`)."""
        return self.local_cluster().region

    def resolve_targets(self, requested: list[str] | None) -> list[Cluster]:
        """Resolve the clusters to act on for a request.

        Args:
            requested: Explicit region names, or None for all configured regions.

        Returns:
            The target clusters.

        Raises:
            ValidationError: If no regions are configured or a name is unknown.
        """
        if not self._clusters:
            raise ValidationError("no regions are configured")
        if not requested:
            return list(self._clusters.values())
        targets = []
        for name in requested:
            cluster = self._clusters.get(name)
            if cluster is None:
                raise ValidationError(f"unknown region: {name}")
            targets.append(cluster)
        return targets

    async def fanout(
        self,
        targets: list[Cluster],
        fn: RegionFn,
        *,
        executor: Executor | None = None,
        read: bool = False,
    ) -> list[RegionStatus]:
        """Run ``fn`` on every target concurrently, collecting per-region results.

        Each call runs in a thread with a timeout; a region that times out or raises
        yields a ``RegionStatus`` with ``message`` set rather than aborting the others.

        Args:
            targets: The clusters to run on.
            fn: The per-region operation returning a RegionStatus.
            executor: Pool to run on; None takes the read pool for a read and the
                default executor otherwise. A stream passes its own so that
                repeating this fan-out for as long as a client stays connected
                cannot starve ordinary requests.
            read: This fan-out serves a page read: run it on the bounded read
                pool and bound each region by ``cluster_read_op_timeout``, so a
                slow cluster costs its own column in the response - which the
                rollup already renders - not the whole page. A write keeps the
                minute-scale ``cluster_op_timeout``: it is worth waiting for.

        Returns:
            One RegionStatus per target.

        Raises:
            ServiceUnavailableError: If ``read`` and the read pool cannot admit
                the whole fan-out.
        """
        timeout = self._read_timeout if read else self._op_timeout
        # For every target before any starts: shed half way through, the regions
        # already running would burn the pool for a result the 503 discards.
        on_pool = read and executor is None
        reserved = self._read_pool.reserve(len(targets)) if on_pool else nullcontext()

        async def run(cluster: Cluster, run_read) -> RegionStatus:
            try:
                # Backstop: a down/slow region fails fast and is reported as an
                # error rather than blocking the whole fan-out indefinitely.
                if run_read is not None:
                    result = run_read(fn, cluster)
                else:
                    result = run_on(executor, fn, cluster)
                return await asyncio.wait_for(result, timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("region %s operation timed out", cluster.region)
                return RegionStatus(
                    region=cluster.region,
                    status="Timeout",
                    message=f"region unreachable (timed out after {timeout}s)",
                )
            except ReadPoolSaturated:
                # The API's condition, not the region's: reported as this
                # request's 503, never as a "Failed" region row.
                raise
            except Exception as exc:  # noqa: BLE001 - surfaced as per-region error
                logger.exception("region %s operation failed", cluster.region)
                return RegionStatus(region=cluster.region, status="Failed", message=str(exc))

        with reserved as run_read:
            return await asyncio.gather(*(run(c, run_read) for c in targets))

    async def gather_each(
        self, targets: list[Cluster], fn: Callable[[Cluster], object]
    ) -> list[tuple[str, object | None]]:
        """Run ``fn`` on each target concurrently, returning ``[(region, result)]``.

        A region whose call fails or times out yields ``(region, None)`` instead of
        aborting the whole fan-out - for reads (e.g. listings) where a down region
        should be skipped, not fatal. Always a read: it runs on the bounded read
        pool under ``cluster_read_op_timeout``.

        Args:
            targets: The clusters to run on.
            fn: The per-region read returning any result.

        Returns:
            One ``(region, result_or_None)`` tuple per target.

        Raises:
            ServiceUnavailableError: If the read pool cannot admit the whole
                fan-out.
        """

        async def run(cluster: Cluster, run_read) -> tuple[str, object | None]:
            try:
                result = await asyncio.wait_for(run_read(fn, cluster), timeout=self._read_timeout)
                return cluster.region, result
            except ReadPoolSaturated:
                raise  # the API's condition, not the region's - see fanout
            except Exception:  # noqa: BLE001 - per-region failure is non-fatal here
                logger.exception("region %s listing failed", cluster.region)
                return cluster.region, None

        # For all of them, or none - see fanout.
        with self._read_pool.reserve(len(targets)) as run_read:
            return await asyncio.gather(*(run(c, run_read) for c in targets))
