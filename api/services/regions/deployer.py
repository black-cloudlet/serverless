"""Multi-region fan-out (docs/ARCHITECTURE.md - Multi-Region).

Every deploy is applied to all target regions concurrently; the per-region
results are rolled up by :mod:`api.services.regions.rollup`. The Kubernetes
client is synchronous, so per-region work runs in threads.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Executor
from contextlib import nullcontext
from typing import Callable

from cloudlet_apis.errors import ServiceUnavailableError
from cloudlet_apis.logging import get_logger

from api.core.config import Settings
from api.models.common import RegionStatus
from api.services.regions.read_pool import ReadPool, ReadPoolSaturated
from api.services.streams.capacity import run_on
from common.cluster import Cluster, NamespacedCluster, clusters_for, select_local
from common.errors import ValidationError

logger = get_logger(__name__)

# fn(cluster) -> RegionStatus  (may run blocking I/O; executed in a thread)
RegionFn = Callable[[NamespacedCluster], RegionStatus]


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
        # The Cluster is cluster-scoped; the namespace is bound HERE, at the one
        # boundary where clusters are handed out, so everything downstream works
        # on a view that cannot mix namespaces mid-operation. Today the binding
        # is the shared workloads namespace; namespace-per-group changes what is
        # bound, not who binds it (docs/proposals/namespace-per-group.md).
        self._namespace: str = settings.workloads_namespace
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

    def local_cluster(self) -> NamespacedCluster:
        """The cluster this API instance sits in, bound to the workloads namespace.

        Selected by config ``local_region`` (matched by region name then cluster
        name), falling back to the first configured region. Used for reads of data
        that is uniform across regions (active/active), to avoid a cross-cluster
        round trip.

        Returns:
            The local cluster, as a namespace-bound view.

        Raises:
            ValidationError: If no regions are configured.
        """
        return NamespacedCluster(select_local(self._clusters, self._local_region), self._namespace)

    def local_region(self) -> str:
        """The name of the local region (see :meth:`local_cluster`)."""
        return self.local_cluster().region

    def resolve_targets(self, requested: list[str] | None) -> list[NamespacedCluster]:
        """Resolve the clusters to act on for a request, namespace-bound.

        Args:
            requested: Explicit region names, or None for all configured regions.

        Returns:
            The target clusters, as namespace-bound views.

        Raises:
            ValidationError: If no regions are configured or a name is unknown.
        """
        if not self._clusters:
            raise ValidationError("no regions are configured")
        if not requested:
            return [NamespacedCluster(c, self._namespace) for c in self._clusters.values()]
        targets = []
        for name in requested:
            cluster = self._clusters.get(name)
            if cluster is None:
                raise ValidationError(f"unknown region: {name}")
            targets.append(NamespacedCluster(cluster, self._namespace))
        return targets

    async def fanout(
        self,
        targets: list[NamespacedCluster],
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

        async def run(cluster: NamespacedCluster, run_read) -> RegionStatus:
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
        self, targets: list[NamespacedCluster], fn: Callable[[NamespacedCluster], object]
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

        async def run(cluster: NamespacedCluster, run_read) -> tuple[str, object | None]:
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
