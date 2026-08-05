"""Multi-site fan-out and status aggregation (docs/ARCHITECTURE.md - Multi-Site)."""

from __future__ import annotations

import asyncio
from typing import Callable

from api.core.config import Settings
from api.models.common import SiteStatus
from common.cluster import Cluster
from common.errors import SiteTotalFailure, ValidationError
from common.logging import get_logger

logger = get_logger(__name__)

# fn(cluster) -> SiteStatus  (may run blocking I/O; executed in a thread)
SiteFn = Callable[[Cluster], SiteStatus]


class Deployer:
    """Owns the per-site cluster clients and runs work across them concurrently."""

    def __init__(self, settings: Settings):
        """Build one cluster client per configured site (connections stay lazy)."""
        self._op_timeout = settings.site_op_timeout
        self._local_site = settings.local_site

        self._clusters: dict[str, Cluster] = {
            site.name: Cluster(site, settings) for site in settings.sites
        }

    def close(self) -> None:
        """Release every site's cluster client (connection pools) at shutdown."""
        for cluster in self._clusters.values():
            cluster.close()

    def local_cluster(self) -> Cluster:
        """The cluster this API instance sits in.

        Raises:
            ValidationError: If no sites are configured.
        """
        if not self._clusters:
            raise ValidationError("no sites are configured")
        if self._local_site:
            by_site = self._clusters.get(self._local_site)
            if by_site:
                return by_site
            for cluster in self._clusters.values():
                if cluster.name == self._local_site:  # match the cluster name too
                    return cluster
        return next(iter(self._clusters.values()))

    def local_site(self) -> str:
        """The name of the local site (see :meth:`local_cluster`)."""
        return self.local_cluster().site

    def resolve_targets(self, requested: list[str] | None) -> list[Cluster]:
        """Resolve the clusters to act on for a request.

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

    async def fanout(self, targets: list[Cluster], fn: SiteFn) -> list[SiteStatus]:
        """Run ``fn`` on every target concurrently, collecting per-site results.

        Each call runs in a thread with a timeout; a site that times out or raises
        yields a ``SiteStatus`` with ``error`` set rather than aborting the others.
        """

        async def run(cluster: Cluster) -> SiteStatus:
            try:
                # Backstop: a down/slow site fails fast and is reported as an
                # error rather than blocking the whole fan-out indefinitely.
                return await asyncio.wait_for(
                    asyncio.to_thread(fn, cluster), timeout=self._op_timeout
                )
            except asyncio.TimeoutError:
                logger.warning("site %s operation timed out", cluster.site)
                return SiteStatus(
                    site=cluster.site,
                    status="Timeout",
                    error=f"site unreachable (timed out after {self._op_timeout}s)",
                )
            except Exception as exc:  # noqa: BLE001 - surfaced as per-site error
                logger.exception("site %s operation failed", cluster.site)
                return SiteStatus(site=cluster.site, status="Failed", error=str(exc))

        return await asyncio.gather(*(run(c) for c in targets))

    async def gather_each(
        self, targets: list[Cluster], fn: Callable[[Cluster], object]
    ) -> list[tuple[str, object | None]]:
        """Run ``fn`` on each target concurrently, returning ``[(site, result)]``."""

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

    Raises:
        SiteTotalFailure: If every site failed.
    """
    if all(s.error is not None for s in statuses):
        raise SiteTotalFailure(
            "Deployment failed in all sites.",
            details=[{"site": s.site, "message": s.error} for s in statuses],
        )
    return overall_status_for_sites(statuses)


def overall_status_for_sites(statuses: list[SiteStatus]) -> str:
    """Roll up SiteStatus objects, mapping an unreachable site to ``Failed``.

    Single projection shared by the create path (aggregate) and the GET read path
    so the two can't drift.
    """
    return overall_status([s.status if s.error is None else "Failed" for s in statuses])


def overall_status(statuses: list[str]) -> str:
    """Collapse per-site KSVC statuses into one overall status (GET / list)."""
    if not statuses:
        return "Degraded"
    if any(s == "Failed" for s in statuses):
        return "Degraded"
    if any(s == "Terminating" for s in statuses):
        return "Terminating"
    if all(s == "Ready" for s in statuses):
        return "Ready"
    return "Deploying"


def status_code_for(overall: str, created: bool) -> int:
    """Map an overall status to an HTTP status code."""
    if overall == "Degraded":
        return 207
    if overall in ("Deploying", "Building"):
        return 202  # accepted, still in flight - a non-terminal poll state
    return 201 if created else 200
