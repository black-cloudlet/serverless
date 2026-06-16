"""CRUD + read-back for API-managed workload secrets & configs (docs §7.3).

These are created and owned by the API directly (never via ESO/Vault), applied
to every zone, and readable back by the owning group.
"""

from __future__ import annotations

import base64

from app.auth.claims import Principal
from app.core.config import Settings, ZoneConfig
from app.core.errors import ForbiddenError, NotFoundError
from app.clients.zone_client import ZoneClient
from app.models.common import LABEL_GROUP, ZoneStatus
from app.models.resource import ResourceCreate, ResourceResponse
from app.services import resources as builders
from app.services.deployer import Deployer, aggregate
from app.services.labels import group_selector

REDACTED = "<redacted>"
_KINDS = {"secret": "Secret", "config": "ConfigMap"}


class ResourceService:
    def __init__(self, settings: Settings, deployer: Deployer):
        self._settings = settings
        self._deployer = deployer

    def _kind(self, rtype: str) -> str:
        return _KINDS[rtype]

    async def create(
        self, rtype: str, spec: ResourceCreate, user: Principal
    ) -> tuple[ResourceResponse, int]:
        group = user.primary_group
        if rtype == "secret":
            manifest = builders.build_secret(spec.name, group, user.username, spec.data)
        else:
            manifest = builders.build_configmap(
                spec.name, group, user.username, spec.data
            )

        def apply(client: ZoneClient, zone: ZoneConfig) -> ZoneStatus:
            client.apply(manifest, zone.namespace)
            return ZoneStatus(zone=zone.name, status="Applied")

        targets = self._deployer.resolve_targets(None)
        statuses = await self._deployer.fanout(targets, apply)
        overall = aggregate(statuses, success_label="Applied")
        body = ResourceResponse(
            name=spec.name,
            type=rtype,  # type: ignore[arg-type]
            keys=sorted(spec.data.keys()),
            overallStatus=overall,
            zones=statuses,
        )
        return body, (207 if overall == "Degraded" else 201)

    async def get(
        self, rtype: str, name: str, user: Principal, reveal: bool = False
    ) -> ResourceResponse:
        kind = self._kind(rtype)
        targets = self._deployer.resolve_targets(None)
        obj: dict | None = None
        statuses: list[ZoneStatus] = []
        for zone in targets:
            try:
                fetched = self._deployer.client(zone).get(
                    "v1", kind, name, zone.namespace
                )
                self._assert_access(fetched, user)
                obj = obj or fetched
                statuses.append(ZoneStatus(zone=zone.name, status="Present"))
            except ForbiddenError:
                raise
            except Exception as exc:  # noqa: BLE001
                statuses.append(ZoneStatus(zone=zone.name, status="Missing", error=str(exc)))
        if obj is None:
            raise NotFoundError(f"{rtype} '{name}' not found")

        data = self._read_data(rtype, obj, reveal)
        return ResourceResponse(
            name=name,
            type=rtype,  # type: ignore[arg-type]
            keys=sorted(data.keys()),
            overallStatus="Present" if all(s.error is None for s in statuses) else "Degraded",
            zones=statuses,
            data=data,
        )

    async def list(self, rtype: str, user: Principal) -> list[str]:
        kind = self._kind(rtype)
        groups = user.groups if not user.is_admin else []
        selector = group_selector(groups) if groups else None
        names: set[str] = set()
        for zone in self._deployer.resolve_targets(None):
            try:
                items = self._deployer.client(zone).list(
                    "v1", kind, zone.namespace, label_selector=selector
                )
                names.update(i["metadata"]["name"] for i in items)
            except Exception:  # noqa: BLE001 - skip unreachable zone in listing
                continue
        return sorted(names)

    async def delete(self, rtype: str, name: str, user: Principal) -> None:
        kind = self._kind(rtype)
        deleted = False
        for zone in self._deployer.resolve_targets(None):
            client = self._deployer.client(zone)
            try:
                obj = client.get("v1", kind, name, zone.namespace)
                self._assert_access(obj, user)
                client.delete("v1", kind, name, zone.namespace)
                deleted = True
            except ForbiddenError:
                raise
            except Exception:  # noqa: BLE001 - absent in this zone
                continue
        if not deleted:
            raise NotFoundError(f"{rtype} '{name}' not found")

    def _read_data(self, rtype: str, obj: dict, reveal: bool) -> dict[str, str]:
        if rtype == "config":
            return dict(obj.get("data", {}) or {})
        # Secret values are base64; redact by default.
        raw = obj.get("data", {}) or {}
        if not reveal:
            return {k: REDACTED for k in raw}
        return {
            k: base64.b64decode(v).decode("utf-8", "replace") for k, v in raw.items()
        }

    def _assert_access(self, obj: dict, user: Principal) -> None:
        labels = (obj.get("metadata", {}) or {}).get("labels", {}) or {}
        group = labels.get(LABEL_GROUP, "")
        if not user.can_access_group(group):
            raise ForbiddenError("not permitted for this resource's group")
