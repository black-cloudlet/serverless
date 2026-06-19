"""Multi-site fan-out and status aggregation (docs §4).

Every deploy is applied to all target sites concurrently; results are aggregated
into a single response. Partial failure -> Degraded (HTTP 207); total failure ->
HTTP 502. The Kubernetes client is synchronous, so per-site work runs in threads.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from app.clients.cluster import Cluster
from app.core.config import Settings
from app.core.errors import ValidationError, SiteTotalFailure
from app.core.logging import get_logger
from app.models.common import SiteStatus

logger = get_logger(__name__)

# fn(cluster) -> SiteStatus  (may run blocking I/O; executed in a thread)
SiteFn = Callable[[Cluster], SiteStatus]


class Deployer:
    def __init__(self, settings: Settings):
        # `settings` is a construction input (each Cluster needs it); we don't
        # retain it — only the per-site timeout and the built Clusters.
        self._op_timeout = settings.site_op_timeout
        # One Cluster per configured site, built once up front (not per request).
        # The k8s connection stays lazy (on first use), so startup doesn't fail
        # if a site is down.
        self._clusters: dict[str, Cluster] = {
            site.name: Cluster(site, settings) for site in settings.sites
        }

    def resolve_targets(self, requested: list[str] | None) -> list[Cluster]:
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
FanoutFn = Callable[[list[Cluster], SiteFn], Awaitable[list[SiteStatus]]]
