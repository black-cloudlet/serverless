"""Multi-region fan-out and status aggregation (docs/ARCHITECTURE.md - Multi-Region).

Every deploy is applied to all target regions concurrently; results are aggregated
into a single response. Partial failure -> Failed (HTTP 207); total failure ->
HTTP 502. The Kubernetes client is synchronous, so per-region work runs in threads.
"""

from __future__ import annotations

import asyncio
import contextvars
from concurrent.futures import Executor, ThreadPoolExecutor
from typing import Callable

from cloudlet_apis.errors import ServiceUnavailableError
from cloudlet_apis.logging import get_logger

from api.core.config import Settings
from api.models.common import RegionStatus
from api.services.streams.capacity import run_on
from common.cluster import Cluster, clusters_for, select_local
from common.errors import RegionTotalFailure, ValidationError

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

    async def run(self, fn, *args):
        """Run one blocking read on the pool, or refuse it.

        Raises:
            ServiceUnavailableError: If ``workers + max_queued`` reads are
                already in flight - abandoned-but-still-running ones included.
        """
        if self._inflight >= self._limit:
            logger.warning("refusing cluster read: %d already in flight", self._inflight)
            raise ReadPoolSaturated("the API is saturated with cluster reads; retry shortly")
        loop = asyncio.get_running_loop()
        self._inflight += 1
        # Submitted directly, and the release hangs on the CONCURRENT future:
        # run_in_executor's asyncio wrapper acknowledges a cancel immediately
        # even while the thread runs on, so a callback there (or a finally on
        # the await) would free the slot the moment a caller's wait_for gave
        # up - long before the worker did. The concurrent future completes only
        # when the thread actually finishes (or was cancelled before starting),
        # which is the occupancy this counts. Context copied as start_on does,
        # so the read's log lines keep the request's correlation id.
        ctx = contextvars.copy_context()
        concurrent_future = self._executor.submit(ctx.run, fn, *args)

        def release(_cf) -> None:
            # Runs on the worker thread; the counter is only touched on the loop.
            try:
                loop.call_soon_threadsafe(self._release)
            except RuntimeError:  # noqa: S110 - loop closed; teardown is under way
                pass

        concurrent_future.add_done_callback(release)
        return await asyncio.wrap_future(concurrent_future)

    def _release(self) -> None:
        """Give the admission back (runs on the loop when the thread finishes)."""
        self._inflight -= 1

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

        For the single-cluster reads that serve GETs (a spec read on the
        representative region), so they share the fan-out's pool and admission
        instead of the process-wide default executor.

        Raises:
            ServiceUnavailableError: If the read pool is saturated.
        """
        return await self._read_pool.run(fn, *args)

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
            ServiceUnavailableError: If ``read`` and the read pool is saturated.
        """
        timeout = self._read_timeout if read else self._op_timeout

        async def run(cluster: Cluster) -> RegionStatus:
            try:
                # Backstop: a down/slow region fails fast and is reported as an
                # error rather than blocking the whole fan-out indefinitely.
                if read and executor is None:
                    result = self._read_pool.run(fn, cluster)
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

        return await asyncio.gather(*(run(c) for c in targets))

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
            ServiceUnavailableError: If the read pool is saturated.
        """

        async def run(cluster: Cluster) -> tuple[str, object | None]:
            try:
                result = await asyncio.wait_for(
                    self._read_pool.run(fn, cluster), timeout=self._read_timeout
                )
                return cluster.region, result
            except ReadPoolSaturated:
                raise  # the API's condition, not the region's - see fanout
            except Exception:  # noqa: BLE001 - per-region failure is non-fatal here
                logger.exception("region %s listing failed", cluster.region)
                return cluster.region, None

        return await asyncio.gather(*(run(c) for c in targets))


def aggregate(statuses: list[RegionStatus]) -> str:
    """Overall status for the create/update path.

    Raises RegionTotalFailure when every region failed; otherwise delegates the rollup
    to overall_status, mapping an unreachable region to ``Failed``. One definition of
    the rollup, shared with the read paths, so the two cannot drift.

    Args:
        statuses: The per-region results of the apply fan-out.

    Returns:
        The overall status (Ready/Deploying/Failed).

    Raises:
        RegionTotalFailure: If every region failed.
    """
    if all(s.message is not None for s in statuses):
        raise RegionTotalFailure(
            "Deployment failed in all regions.",
            details=[{"region": s.region, "message": s.message} for s in statuses],
        )
    return overall_status_for_regions(statuses)


def overall_status_for_regions(statuses: list[RegionStatus]) -> str:
    """Roll up RegionStatus objects, mapping an unreachable region to ``Failed``.

    Single projection shared by the create path (aggregate) and the GET read path
    so the two can't drift.

    Args:
        statuses: The per-region statuses.

    Returns:
        The overall status (Ready/Deploying/Failed).
    """
    return overall_status([s.status if s.message is None else "Failed" for s in statuses])


def overall_status(statuses: list[str]) -> str:
    """Collapse per-region KSVC statuses into one overall status (GET / list).

    A ``Failed`` region makes the whole deployment ``Failed`` - one vocabulary for
    the region rows and the rollup; a ``Terminating`` one makes
    it ``Terminating``. Otherwise all-``Ready`` is ``Ready`` and anything in flight is
    ``Deploying`` - including mixed ``Ready`` + ``Deploying``, a normal rollout with one
    region ahead, NOT a failure. That is what stops a false ``Failed`` while coming up.

    Args:
        statuses: The per-region status strings.

    Returns:
        The overall status (Ready/Deploying/Failed/Terminating).
    """
    if not statuses:
        return "Failed"
    if any(s == "Failed" for s in statuses):
        return "Failed"
    if any(s == "Terminating" for s in statuses):
        return "Terminating"
    if all(s == "Ready" for s in statuses):
        return "Ready"
    return "Deploying"


def status_code_for(overall: str, created: bool) -> int:
    """Map an overall status to an HTTP status code.

    Args:
        overall: The rolled-up status (Ready/Deploying/Failed).
        created: Whether the call created a new workload (vs updated one).

    Returns:
        207 for Failed, 202 for Deploying/Building, 201 for a create, else 200.
    """
    if overall == "Failed":
        return 207
    if overall in ("Deploying", "Building"):
        return 202  # accepted, still in flight - a non-terminal poll state
    return 201 if created else 200
