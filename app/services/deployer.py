"""Multi-site fan-out and status aggregation (docs §4).

Every deploy is applied to all target sites concurrently; results are aggregated
into a single response. Partial failure -> Degraded (HTTP 207); total failure ->
HTTP 502. The Kubernetes client is synchronous, so per-site work runs in threads.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from app.clients.cluster import Cluster
from app.core.config import Settings, SiteConfig
from app.core.errors import ValidationError, SiteTotalFailure
from app.core.logging import get_logger
from app.models.common import SiteStatus

logger = get_logger(__name__)

# fn(cluster, site) -> SiteStatus  (may run blocking I/O; executed in a thread)
SiteFn = Callable[[Cluster, SiteConfig], SiteStatus]


class Deployer:
    def __init__(self, settings: Settings):
        self._settings = settings
        # Build one Cluster per configured site up front (from config), so they
        # are not created per request. The k8s connection itself stays lazy
        # (established on first use) so startup doesn't fail if a site is down.
        self._clusters: dict[str, Cluster] = {
            site.name: self._build(site) for site in settings.sites
        }

    def _build(self, site: SiteConfig) -> Cluster:
        return Cluster(
            site,
            client_cert_path=self._settings.client_cert_path,
            client_key_path=self._settings.client_key_path,
            ca_path=self._settings.ca_bundle.path,
        )

    def cluster(self, site: SiteConfig) -> Cluster:
        cluster = self._clusters.get(site.name)
        if cluster is None:  # site not present at init (defensive)
            cluster = self._clusters[site.name] = self._build(site)
        return cluster

    def resolve_targets(self, requested: list[str] | None) -> list[SiteConfig]:
        if not self._settings.sites:
            raise ValidationError("no sites are configured")
        if not requested:
            return list(self._settings.sites)
        targets = []
        for name in requested:
            site = self._settings.site(name)
            if site is None:
                raise ValidationError(f"unknown site: {name}")
            targets.append(site)
        return targets

    async def fanout(self, targets: list[SiteConfig], fn: SiteFn) -> list[SiteStatus]:
        async def run(site: SiteConfig) -> SiteStatus:
            try:
                return await asyncio.to_thread(fn, self.cluster(site), site)
            except Exception as exc:  # noqa: BLE001 - surfaced as per-site error
                logger.exception("site %s operation failed", site.name)
                return SiteStatus(site=site.name, status="Failed", error=str(exc))

        return await asyncio.gather(*(run(z) for z in targets))


def aggregate(statuses: list[SiteStatus], success_label: str) -> str:
    """Return the overall status, or raise SiteTotalFailure if every site failed."""
    ok = [s for s in statuses if s.error is None]
    if not ok:
        raise SiteTotalFailure(
            "Deployment failed in all sites.",
            details=[{"site": s.site, "message": s.error} for s in statuses],
        )
    return success_label if len(ok) == len(statuses) else "Degraded"


def status_code_for(overall: str, created: bool) -> int:
    if overall == "Degraded":
        return 207
    return 201 if created else 200


# Convenience for routers/tests wanting a fanout helper signature.
FanoutFn = Callable[[list[SiteConfig], SiteFn], Awaitable[list[SiteStatus]]]
