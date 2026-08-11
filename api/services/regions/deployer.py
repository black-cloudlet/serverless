"""Multi-region fan-out and status aggregation (docs/ARCHITECTURE.md - Multi-Region).

Every deploy is applied to all target regions concurrently; results are aggregated
into a single response. Partial failure -> Failed (HTTP 207); total failure ->
HTTP 502. The Kubernetes client is synchronous, so per-region work runs in threads.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Executor
from typing import Callable

from cloudlet_apis.logging import get_logger

from api.core.config import Settings
from api.models.common import RegionStatus
from api.services.streams.capacity import run_on
from common.cluster import Cluster, clusters_for, select_local
from common.errors import RegionTotalFailure, ValidationError

logger = get_logger(__name__)

# fn(cluster) -> RegionStatus  (may run blocking I/O; executed in a thread)
RegionFn = Callable[[Cluster], RegionStatus]


class Deployer:
    """Owns the per-region cluster clients and runs work across them concurrently."""

    def __init__(self, settings: Settings):
        """Build one cluster client per configured region (connections stay lazy).

        Args:
            settings: Global settings; only the per-region timeout, local region, and
                the built Clusters are retained.
        """
        self._op_timeout = settings.region_op_timeout
        self._local_region = settings.local_region
        self._clusters: dict[str, Cluster] = clusters_for(settings)

    def close(self) -> None:
        """Release every region's cluster client (connection pools) at shutdown."""
        for cluster in self._clusters.values():
            cluster.close()

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
        self, targets: list[Cluster], fn: RegionFn, *, executor: Executor | None = None
    ) -> list[RegionStatus]:
        """Run ``fn`` on every target concurrently, collecting per-region results.

        Each call runs in a thread with a timeout; a region that times out or raises
        yields a ``RegionStatus`` with ``message`` set rather than aborting the others.

        Args:
            targets: The clusters to run on.
            fn: The per-region operation returning a RegionStatus.
            executor: Pool to run on; None takes the default one. A stream passes
                its own so that repeating this fan-out for as long as a client
                stays connected cannot starve ordinary requests.

        Returns:
            One RegionStatus per target.
        """

        async def run(cluster: Cluster) -> RegionStatus:
            try:
                # Backstop: a down/slow region fails fast and is reported as an
                # error rather than blocking the whole fan-out indefinitely.
                return await asyncio.wait_for(
                    run_on(executor, fn, cluster), timeout=self._op_timeout
                )
            except asyncio.TimeoutError:
                logger.warning("region %s operation timed out", cluster.region)
                return RegionStatus(
                    region=cluster.region,
                    status="Timeout",
                    message=f"region unreachable (timed out after {self._op_timeout}s)",
                )
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
        should be skipped, not fatal.

        Args:
            targets: The clusters to run on.
            fn: The per-region read returning any result.

        Returns:
            One ``(region, result_or_None)`` tuple per target.
        """

        async def run(cluster: Cluster) -> tuple[str, object | None]:
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(fn, cluster), timeout=self._op_timeout
                )
                return cluster.region, result
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
