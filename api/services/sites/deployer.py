"""Multi-site fan-out and status aggregation (docs/ARCHITECTURE.md - Multi-Site).

Every deploy is applied to all target sites concurrently; results are aggregated
into a single response. Partial failure -> Failed (HTTP 207); total failure ->
HTTP 502. The Kubernetes client is synchronous, so per-site work runs in threads.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Executor
from typing import Callable

from cloudlet_apis.logging import get_logger

from api.core.config import Settings
from api.models.common import SiteStatus
from api.services.streams.capacity import run_on
from common.cluster import Cluster, clusters_for, select_local
from common.errors import SiteTotalFailure, ValidationError

logger = get_logger(__name__)

# fn(cluster) -> SiteStatus  (may run blocking I/O; executed in a thread)
SiteFn = Callable[[Cluster], SiteStatus]


class Deployer:
    """Owns the per-site cluster clients and runs work across them concurrently."""

    def __init__(self, settings: Settings):
        """Build one cluster client per configured site (connections stay lazy).

        Args:
            settings: Global settings; only the per-site timeout, local site, and
                the built Clusters are retained.
        """
        self._op_timeout = settings.site_op_timeout
        self._local_site = settings.local_site
        self._clusters: dict[str, Cluster] = clusters_for(settings)

    def close(self) -> None:
        """Release every site's cluster client (connection pools) at shutdown."""
        for cluster in self._clusters.values():
            cluster.close()

    def local_cluster(self) -> Cluster:
        """The cluster this API instance sits in.

        Selected by config ``local_site`` (matched by site name then cluster
        name), falling back to the first configured site. Used for reads of data
        that is uniform across sites (active/active), to avoid a cross-cluster
        round trip.

        Returns:
            The local cluster.

        Raises:
            ValidationError: If no sites are configured.
        """
        return select_local(self._clusters, self._local_site)

    def local_site(self) -> str:
        """The name of the local site (see :meth:`local_cluster`)."""
        return self.local_cluster().site

    def resolve_targets(self, requested: list[str] | None) -> list[Cluster]:
        """Resolve the clusters to act on for a request.

        Args:
            requested: Explicit site names, or None for all configured sites.

        Returns:
            The target clusters.

        Raises:
            ValidationError: If no sites are configured or a name is unknown.
        """
        if not self._clusters:
            raise ValidationError("no sites are configured")
        if not requested:
            return list(self._clusters.values())
        targets = []
        for name in requested:
            cluster = self._clusters.get(name)
            if cluster is None:
                raise ValidationError(f"unknown site: {name}")
            targets.append(cluster)
        return targets

    async def fanout(
        self, targets: list[Cluster], fn: SiteFn, *, executor: Executor | None = None
    ) -> list[SiteStatus]:
        """Run ``fn`` on every target concurrently, collecting per-site results.

        Each call runs in a thread with a timeout; a site that times out or raises
        yields a ``SiteStatus`` with ``message`` set rather than aborting the others.

        Args:
            targets: The clusters to run on.
            fn: The per-site operation returning a SiteStatus.
            executor: Pool to run on; None takes the default one. A stream passes
                its own so that repeating this fan-out for as long as a client
                stays connected cannot starve ordinary requests.

        Returns:
            One SiteStatus per target.
        """

        async def run(cluster: Cluster) -> SiteStatus:
            try:
                # Backstop: a down/slow site fails fast and is reported as an
                # error rather than blocking the whole fan-out indefinitely.
                return await asyncio.wait_for(
                    run_on(executor, fn, cluster), timeout=self._op_timeout
                )
            except asyncio.TimeoutError:
                logger.warning("site %s operation timed out", cluster.site)
                return SiteStatus(
                    site=cluster.site,
                    status="Timeout",
                    message=f"site unreachable (timed out after {self._op_timeout}s)",
                )
            except Exception as exc:  # noqa: BLE001 - surfaced as per-site error
                logger.exception("site %s operation failed", cluster.site)
                return SiteStatus(site=cluster.site, status="Failed", message=str(exc))

        return await asyncio.gather(*(run(c) for c in targets))

    async def gather_each(
        self, targets: list[Cluster], fn: Callable[[Cluster], object]
    ) -> list[tuple[str, object | None]]:
        """Run ``fn`` on each target concurrently, returning ``[(site, result)]``.

        A site whose call fails or times out yields ``(site, None)`` instead of
        aborting the whole fan-out - for reads (e.g. listings) where a down site
        should be skipped, not fatal.

        Args:
            targets: The clusters to run on.
            fn: The per-site read returning any result.

        Returns:
            One ``(site, result_or_None)`` tuple per target.
        """

        async def run(cluster: Cluster) -> tuple[str, object | None]:
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(fn, cluster), timeout=self._op_timeout
                )
                return cluster.site, result
            except Exception:  # noqa: BLE001 - per-site failure is non-fatal here
                logger.exception("site %s listing failed", cluster.site)
                return cluster.site, None

        return await asyncio.gather(*(run(c) for c in targets))


def aggregate(statuses: list[SiteStatus]) -> str:
    """Overall status for the create/update path.

    Raises SiteTotalFailure when every site failed; otherwise delegates the rollup
    to overall_status, mapping an unreachable site to ``Failed``. One definition of
    the rollup, shared with the read paths, so the two cannot drift.

    Args:
        statuses: The per-site results of the apply fan-out.

    Returns:
        The overall status (Ready/Deploying/Failed).

    Raises:
        SiteTotalFailure: If every site failed.
    """
    if all(s.message is not None for s in statuses):
        raise SiteTotalFailure(
            "Deployment failed in all sites.",
            details=[{"site": s.site, "message": s.message} for s in statuses],
        )
    return overall_status_for_sites(statuses)


def overall_status_for_sites(statuses: list[SiteStatus]) -> str:
    """Roll up SiteStatus objects, mapping an unreachable site to ``Failed``.

    Single projection shared by the create path (aggregate) and the GET read path
    so the two can't drift.

    Args:
        statuses: The per-site statuses.

    Returns:
        The overall status (Ready/Deploying/Failed).
    """
    return overall_status([s.status if s.message is None else "Failed" for s in statuses])


def overall_status(statuses: list[str]) -> str:
    """Collapse per-site KSVC statuses into one overall status (GET / list).

    A ``Failed`` site makes the whole deployment ``Failed`` - one vocabulary for
    the site rows and the rollup; a ``Terminating`` one makes
    it ``Terminating``. Otherwise all-``Ready`` is ``Ready`` and anything in flight is
    ``Deploying`` - including mixed ``Ready`` + ``Deploying``, a normal rollout with one
    site ahead, NOT a failure. That is what stops a false ``Failed`` while coming up.

    Args:
        statuses: The per-site status strings.

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
