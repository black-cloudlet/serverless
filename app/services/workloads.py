"""Workload orchestration: build manifests once, fan out to all sites.

Covers FaaS (build then deploy) and CaaS (deploy image), plus lookup/delete with
group-scoped access control. See docs §3, §4, §6.2.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.auth.claims import Principal
from app.core.config import Settings, SiteConfig
from app.core.errors import ForbiddenError, NotFoundError, ServiceUnavailableError
from app.models.common import (
    LABEL_GROUP,
    WorkloadResponse,
    SiteStatus,
)
from app.models.container import ContainerCreate
from app.models.function import FunctionCreate
from app.services import ksvc as ksvc_svc
from app.services import route as route_svc
from app.services import secrets as secret_svc
from app.services.builder import Builder, BuildRequest
from app.services.deployer import Deployer, aggregate, status_code_for
from app.services.files import resolve_files
from app.clients.cluster import Cluster, ResourceKind

OFFERING_FUNCTION = "faas"
OFFERING_CONTAINER = "caas"


def _ksvc_status(obj: dict) -> tuple[str, str | None]:
    status = obj.get("status", {}) or {}
    conditions = status.get("conditions", []) or []
    ready = next((c for c in conditions if c.get("type") == "Ready"), None)
    revision = status.get("latestReadyRevisionName") or status.get(
        "latestCreatedRevisionName"
    )
    if ready and ready.get("status") == "True":
        return "Ready", revision
    return "Deploying", revision


class WorkloadService:
    def __init__(self, settings: Settings, deployer: Deployer, builder: Builder):
        self._settings = settings
        self._deployer = deployer
        self._builder = builder

    # -- create ----------------------------------------------------------
    async def create_function(
        self, spec: FunctionCreate, user: Principal
    ) -> tuple[WorkloadResponse, int]:
        group = user.primary_group
        try:
            build = self._builder.build(
                BuildRequest(
                    name=spec.name,
                    group=group,
                    git_url=spec.gitUrl,
                    branch=spec.branch,
                    git_token=spec.gitToken,
                    runtime=spec.runtime,
                )
            )
        except NotImplementedError as exc:
            raise ServiceUnavailableError(str(exc)) from exc

        body, code = await self._deploy(
            spec=spec,
            user=user,
            image=build.digest or build.image,
            offering=OFFERING_FUNCTION,
            pull_secret_name=None,
            pull_secret_manifest=None,
        )
        body.type = "function"
        body.runtime = spec.runtime
        body.imageDigest = build.digest
        return body, code

    async def create_container(
        self, spec: ContainerCreate, user: Principal
    ) -> tuple[WorkloadResponse, int]:
        group = user.primary_group
        pull_name = f"{spec.name}-pull"
        pull = secret_svc.build_pull_secret(
            pull_name,
            group,
            user.username,
            self._settings.registry.url,
            spec.registryUsername,
            spec.registryToken,
        )
        body, code = await self._deploy(
            spec=spec,
            user=user,
            image=spec.image,
            offering=OFFERING_CONTAINER,
            pull_secret_name=pull_name,
            pull_secret_manifest=pull,
        )
        body.type = "container"
        body.image = spec.image
        return body, code

    async def _deploy(
        self,
        *,
        spec,
        user: Principal,
        image: str,
        offering: str,
        pull_secret_name: str | None,
        pull_secret_manifest: dict | None,
    ) -> tuple[WorkloadResponse, int]:
        group = user.primary_group
        targets = self._deployer.resolve_targets(spec.sites)
        host = route_svc.host_for(spec.name, group, self._settings.route_domain)

        resolved = resolve_files(spec.name, group, user.username, spec.files)
        ksvc = ksvc_svc.build_ksvc(
            name=spec.name,
            group=group,
            owner=user.username,
            image=image,
            offering=offering,
            env=spec.env,
            volumes=resolved.volumes,
            scaling=spec.scaling,
            pull_secret=pull_secret_name,
        )
        mapping = route_svc.build_domain_mapping(
            name=spec.name, group=group, owner=user.username, offering=offering, host=host
        )
        route = route_svc.build_route(
            name=spec.name,
            group=group,
            owner=user.username,
            offering=offering,
            host=host,
            target_namespace=targets[0].namespace,
        )

        def apply(cluster: Cluster, site: SiteConfig) -> SiteStatus:
            for backing in resolved.backing:
                cluster.apply(backing)
            if pull_secret_manifest:
                cluster.apply(pull_secret_manifest)
            cluster.apply(ksvc)
            cluster.apply(mapping)
            cluster.apply(route, namespace=route_svc.KOURIER_NAMESPACE)
            obj = cluster.get(ResourceKind.KNATIVE_SERVICE, spec.name)
            status, revision = _ksvc_status(obj)
            return SiteStatus(site=cluster.name, status=status, revision=revision)

        statuses = await self._deployer.fanout(targets, apply)
        overall = aggregate(statuses, success_label="Ready")
        body = WorkloadResponse(
            name=spec.name,
            type="function",
            url=f"https://{host}",
            overallStatus=overall,
            sites=statuses,
            createdAt=datetime.now(timezone.utc),
        )
        return body, status_code_for(overall, created=True)

    # -- read / delete ---------------------------------------------------
    async def get(
        self, kind: str, name: str, user: Principal
    ) -> WorkloadResponse:
        offering = OFFERING_FUNCTION if kind == "function" else OFFERING_CONTAINER

        def fetch(cluster: Cluster, site: SiteConfig) -> SiteStatus:
            obj = cluster.get(ResourceKind.KNATIVE_SERVICE, name)
            self._assert_access(obj, user)
            status, revision = _ksvc_status(obj)
            return SiteStatus(site=cluster.name, status=status, revision=revision)

        targets = self._deployer.resolve_targets(None)
        statuses = await self._deployer.fanout(targets, fetch)
        if all(s.error is not None for s in statuses):
            raise NotFoundError(f"{kind} '{name}' not found")
        group = user.primary_group
        host = route_svc.host_for(name, group, self._settings.route_domain)
        ok = [s for s in statuses if s.error is None]
        overall = "Ready" if all(s.status == "Ready" for s in ok) else "Degraded"
        return WorkloadResponse(
            name=name,
            type=kind,  # type: ignore[arg-type]
            url=f"https://{host}",
            overallStatus=overall,
            sites=statuses,
        )

    async def delete(self, kind: str, name: str, user: Principal) -> None:
        def remove(cluster: Cluster, site: SiteConfig) -> SiteStatus:
            obj = cluster.get(ResourceKind.KNATIVE_SERVICE, name)
            self._assert_access(obj, user)
            cluster.delete(ResourceKind.KNATIVE_SERVICE, name)
            return SiteStatus(site=cluster.name, status="Deleted")

        targets = self._deployer.resolve_targets(None)
        statuses = await self._deployer.fanout(targets, remove)
        if all(s.error is not None for s in statuses):
            raise NotFoundError(f"{kind} '{name}' not found")

    def _assert_access(self, obj: dict, user: Principal) -> None:
        labels = (obj.get("metadata", {}) or {}).get("labels", {}) or {}
        group = labels.get(LABEL_GROUP, "")
        if not user.can_access_group(group):
            raise ForbiddenError("not permitted for this resource's group")
