"""Multi-zone fan-out and status aggregation (docs §4).

Every deploy is applied to all target zones concurrently; results are aggregated
into a single response. Partial failure -> Degraded (HTTP 207); total failure ->
HTTP 502. The Kubernetes client is synchronous, so per-zone work runs in threads.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from app.clients.cluster import Cluster
from app.core.config import Settings, ZoneConfig
from app.core.errors import ValidationError, ZoneTotalFailure
from app.core.logging import get_logger
from app.models.common import ZoneStatus

logger = get_logger(__name__)

# fn(cluster, zone) -> ZoneStatus  (may run blocking I/O; executed in a thread)
ZoneFn = Callable[[Cluster, ZoneConfig], ZoneStatus]


class Deployer:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._clusters: dict[str, Cluster] = {}

    def cluster(self, zone: ZoneConfig) -> Cluster:
        if zone.name not in self._clusters:
            self._clusters[zone.name] = Cluster(zone)
        return self._clusters[zone.name]

    def resolve_targets(self, requested: list[str] | None) -> list[ZoneConfig]:
        if not self._settings.zones:
            raise ValidationError("no zones are configured")
        if not requested:
            return list(self._settings.zones)
        targets = []
        for name in requested:
            zone = self._settings.zone(name)
            if zone is None:
                raise ValidationError(f"unknown zone: {name}")
            targets.append(zone)
        return targets

    async def fanout(self, targets: list[ZoneConfig], fn: ZoneFn) -> list[ZoneStatus]:
        async def run(zone: ZoneConfig) -> ZoneStatus:
            try:
                return await asyncio.to_thread(fn, self.cluster(zone), zone)
            except Exception as exc:  # noqa: BLE001 - surfaced as per-zone error
                logger.exception("zone %s operation failed", zone.name)
                return ZoneStatus(zone=zone.name, status="Failed", error=str(exc))

        return await asyncio.gather(*(run(z) for z in targets))


def aggregate(statuses: list[ZoneStatus], success_label: str) -> str:
    """Return the overall status, or raise ZoneTotalFailure if every zone failed."""
    ok = [s for s in statuses if s.error is None]
    if not ok:
        raise ZoneTotalFailure(
            "Deployment failed in all zones.",
            details=[{"zone": s.zone, "message": s.error} for s in statuses],
        )
    return success_label if len(ok) == len(statuses) else "Degraded"


def status_code_for(overall: str, created: bool) -> int:
    if overall == "Degraded":
        return 207
    return 201 if created else 200


# Convenience for routers/tests wanting a fanout helper signature.
FanoutFn = Callable[[list[ZoneConfig], ZoneFn], Awaitable[list[ZoneStatus]]]
