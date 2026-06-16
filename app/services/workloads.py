"""Workload orchestration: build manifests once, fan out to all sites.

Covers FaaS (build then deploy) and CaaS (deploy image), plus lookup/delete with
group-scoped access control. See docs §3, §4, §6.2.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.auth.claims import Principal
from app.core.config import Settings, SiteConfig
from app.core.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.models.common import (
    ANNOTATION_HOST,
    LABEL_GROUP,
    LABEL_OFFERING,
    LABEL_WORKLOAD,
    WorkloadResponse,
    SiteStatus,
)
from app.models.container import ContainerCreate, ContainerUpdate
from app.models.function import FunctionCreate, FunctionUpdate
from app.services import ksvc as ksvc_svc
from app.services import route as route_svc
from app.services import secrets as secret_svc
from app.services.builder import Builder, BuildRequest
from app.services.deployer import Deployer, aggregate, status_code_for
from app.services.env import resolve_env
from app.services.files import resolve_files
from app.services.labels import workload_labels
from app.clients.cluster import Cluster, ResourceKind

OFFERING_FUNCTION = "faas"
OFFERING_CONTAINER = "caas"


def object_name(name: str, group: str) -> str:
    """The OpenShift name of a workload and its derived resources: {name}-{group}."""
    return f"{name}-{group}"


def _extract_image(obj: dict) -> str | None:
    containers = (
        ((obj.get("spec", {}) or {}).get("template", {}) or {}).get("spec", {}) or {}
    ).get("containers", []) or []
    return containers[0].get("image") if containers else None


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
        oname = object_name(spec.name, group)
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

        await self._assert_workload_absent(
            spec.name, oname, self._deployer.resolve_targets(spec.sites)
        )
        body, code = await self._apply_workload(
            name=spec.name,
            user=user,
            image=build.digest or build.image,
            offering=OFFERING_FUNCTION,
            env=spec.env,
            files=spec.files,
            scaling=spec.scaling,
            hostname=spec.hostname,
            sites=spec.sites,
            pull_secret_name=None,
            pull_secret_manifest=None,
            created=True,
        )
        body.type = "function"
        body.runtime = spec.runtime
        body.imageDigest = build.digest
        return body, code

    async def create_container(
        self, spec: ContainerCreate, user: Principal
    ) -> tuple[WorkloadResponse, int]:
        group = user.primary_group
        oname = object_name(spec.name, group)
        pull_name = f"{oname}-pull"
        pull = secret_svc.build_pull_secret(
            pull_name,
            workload_labels(group, user.username, oname, OFFERING_CONTAINER),
            self._settings.registry.url,
            spec.registryUsername,
            spec.registryToken,
        )
        await self._assert_workload_absent(
            spec.name, oname, self._deployer.resolve_targets(spec.sites)
        )
        body, code = await self._apply_workload(
            name=spec.name,
            user=user,
            image=spec.image,
            offering=OFFERING_CONTAINER,
            env=spec.env,
            files=spec.files,
            scaling=spec.scaling,
            hostname=spec.hostname,
            sites=spec.sites,
            pull_secret_name=pull_name,
            pull_secret_manifest=pull,
            created=True,
        )
        body.type = "container"
        body.image = spec.image
        return body, code

    # -- update (full replace of the mutable spec) -----------------------
    async def update_function(
        self, name: str, spec: FunctionUpdate, user: Principal
    ) -> tuple[WorkloadResponse, int]:
        existing = await self._load_existing(name, OFFERING_FUNCTION, user)
        body, code = await self._apply_workload(
            name=name,
            user=user,
            image=existing["image"],  # code changes go through a (re)build flow
            offering=OFFERING_FUNCTION,
            env=spec.env,
            files=spec.files,
            scaling=spec.scaling,
            hostname=spec.hostname,
            sites=None,
            pull_secret_name=None,
            pull_secret_manifest=None,
            created=False,
        )
        body.type = "function"
        return body, code

    async def update_container(
        self, name: str, spec: ContainerUpdate, user: Principal
    ) -> tuple[WorkloadResponse, int]:
        existing = await self._load_existing(name, OFFERING_CONTAINER, user)
        oname = object_name(name, user.primary_group)
        image = spec.image or existing["image"]
        body, code = await self._apply_workload(
            name=name,
            user=user,
            image=image,
            offering=OFFERING_CONTAINER,
            env=spec.env,
            files=spec.files,
            scaling=spec.scaling,
            hostname=spec.hostname,
            sites=None,
            pull_secret_name=f"{oname}-pull",  # reuse the existing pull secret
            pull_secret_manifest=None,
            created=False,
        )
        body.type = "container"
        body.image = image
        return body, code

    async def _apply_workload(
        self,
        *,
        name: str,
        user: Principal,
        image: str,
        offering: str,
        env,
        files,
        scaling,
        hostname,
        sites,
        pull_secret_name: str | None,
        pull_secret_manifest: dict | None,
        created: bool,
    ) -> tuple[WorkloadResponse, int]:
        group = user.primary_group
        oname = object_name(name, group)
        targets = self._deployer.resolve_targets(sites)
        host = hostname or route_svc.host_for(name, group, self._settings.route_domain)

        # The DomainMapping name IS the host, so an idempotent apply would hijack
        # another workload's mapping. Reject a host already owned by someone else.
        await self._assert_host_available(host, oname, targets)

        resolved = resolve_files(oname, group, user.username, files)
        resolved_env = resolve_env(oname, group, user.username, env)
        backing = resolved.backing + resolved_env.backing
        ksvc = ksvc_svc.build_ksvc(
            name=oname,
            group=group,
            owner=user.username,
            image=image,
            offering=offering,
            host=host,
            env=resolved_env.env,
            volumes=resolved.volumes,
            scaling=scaling,
            pull_secret=pull_secret_name,
        )
        mapping = route_svc.build_domain_mapping(
            name=oname, group=group, owner=user.username, offering=offering, host=host
        )

        def apply(cluster: Cluster, site: SiteConfig) -> SiteStatus:
            for manifest in backing:
                cluster.apply(manifest)
            if pull_secret_manifest:
                cluster.apply(pull_secret_manifest)
            cluster.apply(ksvc)
            # DomainMapping exposes the custom host; the Serverless Operator
            # auto-creates the OpenShift Route for it.
            cluster.apply(mapping)
            obj = cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
            status, revision = _ksvc_status(obj)
            return SiteStatus(site=cluster.name, status=status, revision=revision)

        statuses = await self._deployer.fanout(targets, apply)
        overall = aggregate(statuses, success_label="Ready")
        body = WorkloadResponse(
            name=name,
            type="function",
            url=f"https://{host}",
            overallStatus=overall,
            sites=statuses,
            createdAt=datetime.now(timezone.utc) if created else None,
        )
        return body, status_code_for(overall, created=created)

    async def _load_existing(
        self, name: str, offering: str, user: Principal
    ) -> dict:
        """Fetch an existing workload (offering-scoped); return {'image','host'}.

        Raises NotFoundError if it doesn't exist, isn't this offering, or the
        caller can't access its group.
        """
        oname = object_name(name, user.primary_group)
        holder: dict = {}

        def fetch(cluster: Cluster, site: SiteConfig) -> SiteStatus:
            obj = cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
            self._assert_access(obj, user)
            self._assert_offering(obj, offering)
            image = _extract_image(obj)
            if image and "image" not in holder:
                holder["image"] = image
            return SiteStatus(site=cluster.name, status="Present")

        statuses = await self._deployer.fanout(
            self._deployer.resolve_targets(None), fetch
        )
        if "image" not in holder:
            raise NotFoundError(f"{offering} workload '{name}' not found")
        return holder

    async def _assert_host_available(
        self, host: str, oname: str, targets: list[SiteConfig]
    ) -> None:
        """Raise ConflictError if `host` is a DomainMapping owned by another workload."""

        def check(cluster: Cluster, site: SiteConfig) -> SiteStatus:
            try:
                existing = cluster.get(ResourceKind.DOMAIN_MAPPING, host)
            except Exception:
                return SiteStatus(site=cluster.name, status="Available")
            labels = (existing.get("metadata", {}) or {}).get("labels", {}) or {}
            owner_workload = labels.get(LABEL_WORKLOAD)
            status = "Available" if owner_workload == oname else "Taken"
            return SiteStatus(site=cluster.name, status=status)

        statuses = await self._deployer.fanout(targets, check)
        if any(s.status == "Taken" for s in statuses):
            raise ConflictError(f"hostname '{host}' is already assigned")

    async def _assert_workload_absent(
        self, name: str, oname: str, targets: list[SiteConfig]
    ) -> None:
        """Raise ConflictError if a workload named `oname` already exists (create only)."""

        def probe(cluster: Cluster, site: SiteConfig) -> SiteStatus:
            try:
                cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
                return SiteStatus(site=cluster.name, status="Exists")
            except Exception:
                return SiteStatus(site=cluster.name, status="Absent")

        statuses = await self._deployer.fanout(targets, probe)
        if any(s.status == "Exists" for s in statuses):
            raise ConflictError(f"workload '{name}' already exists")

    # -- read / delete ---------------------------------------------------
    async def get(
        self, kind: str, name: str, user: Principal
    ) -> WorkloadResponse:
        offering = OFFERING_FUNCTION if kind == "function" else OFFERING_CONTAINER
        oname = object_name(name, user.primary_group)
        host_holder: dict[str, str] = {}

        def fetch(cluster: Cluster, site: SiteConfig) -> SiteStatus:
            obj = cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
            self._assert_access(obj, user)
            self._assert_offering(obj, offering)
            annotations = (obj.get("metadata", {}) or {}).get("annotations", {}) or {}
            if ANNOTATION_HOST in annotations:
                host_holder["host"] = annotations[ANNOTATION_HOST]
            status, revision = _ksvc_status(obj)
            return SiteStatus(site=cluster.name, status=status, revision=revision)

        targets = self._deployer.resolve_targets(None)
        statuses = await self._deployer.fanout(targets, fetch)
        if all(s.error is not None for s in statuses):
            raise NotFoundError(f"{kind} '{name}' not found")
        host = host_holder.get(
            "host", route_svc.host_for(name, user.primary_group, self._settings.route_domain)
        )
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
        offering = OFFERING_FUNCTION if kind == "function" else OFFERING_CONTAINER
        oname = object_name(name, user.primary_group)

        def remove(cluster: Cluster, site: SiteConfig) -> SiteStatus:
            obj = cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
            self._assert_access(obj, user)
            self._assert_offering(obj, offering)
            cluster.delete(ResourceKind.KNATIVE_SERVICE, oname)
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

    def _assert_offering(self, obj: dict, offering: str) -> None:
        """Ensure the object is the expected offering, so /functions can't act on
        a container of the same name (and vice versa). Raises NotFoundError."""
        labels = (obj.get("metadata", {}) or {}).get("labels", {}) or {}
        if labels.get(LABEL_OFFERING) != offering:
            raise NotFoundError("workload not found")
