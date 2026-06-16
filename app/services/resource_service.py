"""CRUD + read-back for API-managed workload secrets & configs (docs §7.3).

These are created and owned by the API directly (never via ESO/Vault), applied
to every site, and readable back by the owning group.
"""

from __future__ import annotations

import base64

from app.auth.claims import Principal
from app.core.config import Settings, SiteConfig
from app.core.errors import ForbiddenError, NotFoundError
from app.clients.cluster import Cluster, ResourceKind
from app.models.common import LABEL_GROUP, SiteStatus
from app.models.resource import ResourceCreate, ResourceResponse
from app.services import resources as builders
from app.services.deployer import Deployer, aggregate
from app.services.labels import group_selector

REDACTED = "<redacted>"
_KINDS = {"secret": ResourceKind.SECRET, "config": ResourceKind.CONFIG_MAP}


class ResourceService:
    def __init__(self, settings: Settings, deployer: Deployer):
        self._settings = settings
        self._deployer = deployer

    def _kind(self, rtype: str) -> ResourceKind:
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

        def apply(cluster: Cluster, site: SiteConfig) -> SiteStatus:
            cluster.apply(manifest)
            return SiteStatus(site=cluster.name, status="Applied")

        targets = self._deployer.resolve_targets(None)
        statuses = await self._deployer.fanout(targets, apply)
        overall = aggregate(statuses, success_label="Applied")
        body = ResourceResponse(
            name=spec.name,
            type=rtype,  # type: ignore[arg-type]
            keys=sorted(spec.data.keys()),
            overallStatus=overall,
            sites=statuses,
        )
        return body, (207 if overall == "Degraded" else 201)

    async def get(
        self, rtype: str, name: str, user: Principal, reveal: bool = False
    ) -> ResourceResponse:
        kind = self._kind(rtype)
        targets = self._deployer.resolve_targets(None)
        obj: dict | None = None
        statuses: list[SiteStatus] = []
        for site in targets:
            try:
                fetched = self._deployer.cluster(site).get(kind, name)
                self._assert_access(fetched, user)
                obj = obj or fetched
                statuses.append(SiteStatus(site=site.name, status="Present"))
            except ForbiddenError:
                raise
            except Exception as exc:  # noqa: BLE001
                statuses.append(SiteStatus(site=site.name, status="Missing", error=str(exc)))
        if obj is None:
            raise NotFoundError(f"{rtype} '{name}' not found")

        data = self._read_data(rtype, obj, reveal)
        return ResourceResponse(
            name=name,
            type=rtype,  # type: ignore[arg-type]
            keys=sorted(data.keys()),
            overallStatus="Present" if all(s.error is None for s in statuses) else "Degraded",
            sites=statuses,
            data=data,
        )

    async def list(self, rtype: str, user: Principal) -> list[str]:
        kind = self._kind(rtype)
        groups = user.groups if not user.is_admin else []
        selector = group_selector(groups) if groups else None
        names: set[str] = set()
        for site in self._deployer.resolve_targets(None):
            try:
                items = self._deployer.cluster(site).list(
                    kind, label_selector=selector
                )
                names.update(i["metadata"]["name"] for i in items)
            except Exception:  # noqa: BLE001 - skip unreachable site in listing
                continue
        return sorted(names)

    async def delete(self, rtype: str, name: str, user: Principal) -> None:
        kind = self._kind(rtype)
        deleted = False
        for site in self._deployer.resolve_targets(None):
            cluster = self._deployer.cluster(site)
            try:
                obj = cluster.get(kind, name)
                self._assert_access(obj, user)
                cluster.delete(kind, name)
                deleted = True
            except ForbiddenError:
                raise
            except Exception:  # noqa: BLE001 - absent in this site
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
