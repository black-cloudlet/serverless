"""Shared workload engine: build manifests once, fan out to all sites.

Offering-agnostic. :class:`~app.services.function.FunctionService` and
:class:`~app.services.container.ContainerService` compose this engine and
add only the offering-specific prep (build-from-Git vs image + pull secret);
everything else — apply, host/absence checks, access control, get/delete — lives
here. See docs §3, §4, §6.2.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.auth.claims import Principal
from app.core.config import Settings
from app.core.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    SiteTotalFailure,
    ValidationError,
)
from app.core.logging import get_logger
from app.models.common import (
    ANNOTATION_GIT_BRANCH,
    ANNOTATION_GIT_URL,
    ANNOTATION_HOST,
    ANNOTATION_RUNTIME,
    ANNOTATION_SIZE,
    LABEL_GROUP,
    LABEL_OFFERING,
    LABEL_WORKLOAD,
    WorkloadResponse,
    WorkloadSummary,
    SiteStatus,
)
from app.models.container import ContainerResponse
from app.models.function import FunctionResponse
from app.services import describe as describe_svc
from app.services import ksvc as ksvc_svc
from app.services import metrics as metrics_svc
from app.services import route as route_svc
from app.services import secrets as secret_svc
from app.services.builder import Builder
from app.services.deployer import Deployer, aggregate, status_code_for
from app.services.env import resolve_env
from app.services.files import resolve_files
from app.clients.cluster import Cluster, ResourceKind

logger = get_logger(__name__)

OFFERING_FUNCTION = "function"
OFFERING_CONTAINER = "container"


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
    """Offering-agnostic orchestration shared by the function/container services."""

    def __init__(self, settings: Settings, deployer: Deployer, builder: Builder):
        self.settings = settings
        self.deployer = deployer
        self.builder = builder

    # -- access control --------------------------------------------------
    def assert_group(self, user: Principal, group: str) -> None:
        """Reject the request unless the caller is a member of ``group`` (admins
        may act for any group). The group is caller-supplied, so this is checked
        on every entry point."""
        if not user.can_access_group(group):
            raise ForbiddenError(f"not a member of group '{group}'")

    # -- async accept helpers --------------------------------------------
    def host_for(self, name: str, hostname: str | None, group: str) -> str:
        """Resolve the external host for a workload, validating any custom one.

        - no hostname -> the default ``{name}-{group}.{route_domain}``
        - a single label -> the base domain is appended (``{label}.{route_domain}``)
        - an FQDN -> accepted only if it is exactly one label under the base
          domain (``{label}.{route_domain}``); deeper names are rejected
        Anything else raises ValidationError (surfaced synchronously as 400).
        """
        domain = self.settings.route_domain
        if not hostname:
            return route_svc.host_for(name, group, domain)
        if "." not in hostname:
            label = hostname
        elif hostname.endswith(f".{domain}"):
            label = hostname[: -len(domain) - 1]  # strip ".{domain}"
        else:
            raise ValidationError(f"hostname must be a single label under '{domain}'")
        if not label or "." in label:
            raise ValidationError(
                f"hostname must be exactly one label under '{domain}'"
            )
        return f"{label}.{domain}"

    def accepted(
        self, kind: str, name: str, group: str, host: str, **extra
    ) -> WorkloadResponse:
        cls = FunctionResponse if kind == OFFERING_FUNCTION else ContainerResponse
        return cls(
            name=name,
            type=kind,
            url=f"https://{host}",
            overallStatus="Pending",
            sites=[],
            statusUrl=f"/api/v1/{kind}s/{name}?group={group}",
            **extra,
        )

    async def run(self, fn, *args) -> None:
        """Run background work; failures surface via status polling, not the caller."""
        try:
            await fn(*args)
        except Exception:  # noqa: BLE001 - background work; surfaced via status polling
            logger.exception("background deploy failed for %s", args)

    # -- apply (full replace of the mutable spec) ------------------------
    async def apply_workload(
        self,
        *,
        name: str,
        user: Principal,
        group: str,
        image: str,
        offering: str,
        env,
        files,
        scaling,
        size,
        hostname,
        sites,
        pull_secret_name: str | None,
        pull_secret_manifest: dict | None,
        created: bool,
        runtime: str | None = None,
        git_url: str | None = None,
        branch: str | None = None,
    ) -> tuple[WorkloadResponse, int]:
        self.assert_group(user, group)
        oname = object_name(name, group)
        targets = self.deployer.resolve_targets(sites)
        host = self.host_for(name, hostname, group)

        # The DomainMapping name IS the host, so an idempotent apply would hijack
        # another workload's mapping. Reject a host already owned by someone else.
        await self.assert_host_available(host, oname, targets)

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
            size=size,
            pull_secret=pull_secret_name,
            runtime=runtime,
            git_url=git_url,
            branch=branch,
            ca_config_map=self.settings.ca_bundle.config_map,
            ca_mount_path=self.settings.ca_bundle.mount_path,
        )
        mapping = route_svc.build_domain_mapping(
            name=oname, group=group, owner=user.username, offering=offering, host=host
        )

        def apply(cluster: Cluster) -> SiteStatus:
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
            return SiteStatus(site=cluster.site, status=status, revision=revision)

        statuses = await self.deployer.fanout(targets, apply)
        overall = aggregate(statuses, success_label="Ready")
        common = dict(
            name=name,
            type=offering,
            url=f"https://{host}",
            overallStatus=overall,
            size=size,
            sites=statuses,
            scaling=scaling,
            env=describe_svc.redact_env(env),
            files=describe_svc.redact_files(files),
            createdAt=datetime.now(timezone.utc) if created else None,
        )
        if offering == OFFERING_FUNCTION:
            body: WorkloadResponse = FunctionResponse(
                **common, runtime=runtime, gitRepo=git_url, branch=branch
            )
        else:
            body = ContainerResponse(**common, image=image)
        return body, status_code_for(overall, created=created)

    async def load_existing(
        self, name: str, offering: str, user: Principal, group: str
    ) -> dict:
        """Fetch an existing workload (offering-scoped); return {'image','host'}.

        Raises NotFoundError if it doesn't exist, isn't this offering, or the
        caller can't access its group.
        """
        self.assert_group(user, group)
        oname = object_name(name, group)
        holder: dict = {}

        def fetch(cluster: Cluster) -> SiteStatus:
            obj = cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
            self._assert_access(obj, user)
            self._assert_offering(obj, offering)
            image = _extract_image(obj)
            if image and "image" not in holder:
                holder["image"] = image
                ann = (obj.get("metadata", {}) or {}).get("annotations", {}) or {}
                # carry build metadata + pull secret forward across updates so a
                # config-only update doesn't drop them
                holder["runtime"] = ann.get(ANNOTATION_RUNTIME)
                holder["gitUrl"] = ann.get(ANNOTATION_GIT_URL)
                holder["branch"] = ann.get(ANNOTATION_GIT_BRANCH)
                holder["pull_secret"] = describe_svc.pull_secret_name(obj)
            return SiteStatus(site=cluster.site, status="Present")

        await self.deployer.fanout(self.deployer.resolve_targets(None), fetch)
        if "image" not in holder:
            raise NotFoundError(f"{offering} workload '{name}' not found")
        return holder

    async def assert_host_available(self, host: str, oname: str, targets: list[Cluster]) -> None:
        """Raise ConflictError if `host` is a DomainMapping owned by another workload."""

        def check(cluster: Cluster) -> SiteStatus:
            try:
                existing = cluster.get(ResourceKind.DOMAIN_MAPPING, host)
            except Exception:
                return SiteStatus(site=cluster.site, status="Available")
            labels = (existing.get("metadata", {}) or {}).get("labels", {}) or {}
            owner_workload = labels.get(LABEL_WORKLOAD)
            status = "Available" if owner_workload == oname else "Taken"
            return SiteStatus(site=cluster.site, status=status)

        statuses = await self.deployer.fanout(targets, check)
        if any(s.status == "Taken" for s in statuses):
            raise ConflictError(f"hostname '{host}' is already assigned")

    async def assert_workload_absent(self, name: str, oname: str, targets: list[Cluster]) -> None:
        """Raise ConflictError if a workload named `oname` already exists (create only)."""

        def probe(cluster: Cluster) -> SiteStatus:
            try:
                cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
                return SiteStatus(site=cluster.site, status="Exists")
            except Exception:
                return SiteStatus(site=cluster.site, status="Absent")

        statuses = await self.deployer.fanout(targets, probe)
        if any(s.status == "Exists" for s in statuses):
            raise ConflictError(f"workload '{name}' already exists")

    # -- read / delete (offering-scoped via kind) ------------------------
    async def get(
        self, kind: str, name: str, user: Principal, group: str
    ) -> WorkloadResponse:
        self.assert_group(user, group)
        offering = kind  # the API kind ("function"/"container") is the offering label
        oname = object_name(name, group)
        meta_holder: dict[str, str] = {}
        # Each OK site donates its KSVC; the spec is uniform across sites, so we
        # read the desired-state spec back from the local site (most reliable hop)
        # once, after the fan-out — see _pick_rep.
        reps: dict[str, tuple] = {}

        def fetch(cluster: Cluster) -> SiteStatus:
            obj = cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
            self._assert_access(obj, user)
            self._assert_offering(obj, offering)
            annotations = (obj.get("metadata", {}) or {}).get("annotations", {}) or {}
            for key, ann in (("host", ANNOTATION_HOST), ("size", ANNOTATION_SIZE)):
                if ann in annotations and key not in meta_holder:
                    meta_holder[key] = annotations[ann]
            reps[cluster.site] = (obj, cluster)
            status, revision = _ksvc_status(obj)
            return SiteStatus(
                site=cluster.site,
                status=status,
                revision=revision,
                replicas=self._site_replicas(cluster, revision),
                usage=self._site_usage(cluster, oname),
            )

        targets = self.deployer.resolve_targets(None)
        statuses = await self.deployer.fanout(targets, fetch)
        if all(s.error is not None for s in statuses):
            raise NotFoundError(f"{kind} '{name}' not found")
        host = meta_holder.get(
            "host", route_svc.host_for(name, group, self.settings.route_domain)
        )
        ok = [s for s in statuses if s.error is None]
        overall = "Ready" if all(s.status == "Ready" for s in ok) else "Degraded"
        spec = None
        image = runtime = None
        if reps:
            # the desired-state spec is uniform across sites: read it from the
            # local site if it answered, else any site that did.
            obj, cluster = reps.get(self.deployer.local_site()) or next(iter(reps.values()))
            image = _extract_image(obj)
            runtime = ((obj.get("metadata", {}) or {}).get("annotations", {}) or {}).get(
                ANNOTATION_RUNTIME
            )
            spec = await asyncio.to_thread(self._describe_spec, cluster, obj)
        common = dict(
            name=name,
            type=kind,
            url=f"https://{host}",
            overallStatus=overall,
            size=meta_holder.get("size"),
            sites=statuses,
            scaling=spec.scaling if spec else None,
            env=spec.env if spec else [],
            files=spec.files if spec else [],
        )
        if kind == OFFERING_FUNCTION:
            # no image: the built image is internal, the client deals in source
            return FunctionResponse(
                **common,
                runtime=runtime,
                gitRepo=spec.gitRepo if spec else None,
                branch=spec.branch if spec else None,
            )
        return ContainerResponse(
            **common,
            image=image,
            registryUsername=spec.registryUsername if spec else None,
        )

    def _describe_spec(self, cluster: Cluster, obj: dict):
        """Read the desired-state spec (secrets redacted) from a KSVC. Fetches the
        file ConfigMap(s) for non-secret file contents and the pull secret for the
        registry username (never the token). Best-effort: a failed read just leaves
        the corresponding field null."""
        configmaps: dict[str, dict] = {}
        for cm_name in describe_svc.configmap_refs(obj):
            try:
                cm = cluster.get(ResourceKind.CONFIG_MAP, cm_name)
                configmaps[cm_name] = cm.get("data") or {}
            except Exception:  # noqa: BLE001 - content is best-effort
                pass
        registry_username = None
        ps_name = describe_svc.pull_secret_name(obj)
        if ps_name:
            try:
                secret = cluster.get(ResourceKind.SECRET, ps_name)
                registry_username = secret_svc.registry_username(secret)
            except Exception:  # noqa: BLE001 - username is best-effort
                pass
        return describe_svc.parse_spec(obj, configmaps, registry_username=registry_username)

    def _site_replicas(self, cluster: Cluster, revision: str | None) -> int | None:
        """Best-effort running pod count from the revision the KSVC points at —
        the autoscaler's authoritative scale (`Revision.status.actualReplicas`),
        which doesn't depend on the metrics API. None if it can't be read."""
        if not revision:
            return None
        try:
            rev = cluster.get(ResourceKind.KNATIVE_REVISION, revision)
            return (rev.get("status", {}) or {}).get("actualReplicas")
        except Exception:  # noqa: BLE001 - best-effort, never fatal
            return None

    def _site_usage(self, cluster: Cluster, oname: str):
        """Best-effort live cpu/memory summed over the workload's running pods.
        None if the metrics API is unavailable or the workload is scaled to
        zero (no running pods)."""
        try:
            items = cluster.get(
                ResourceKind.POD_METRICS,
                label_selector=f"serving.knative.dev/service={oname}",
            )
            return metrics_svc.sum_usage(items)
        except Exception:  # noqa: BLE001 - usage is best-effort, never fatal
            return None

    async def delete(
        self, kind: str, name: str, user: Principal, group: str
    ) -> None:
        self.assert_group(user, group)
        offering = kind  # the API kind ("function"/"container") is the offering label
        oname = object_name(name, group)

        def remove(cluster: Cluster) -> SiteStatus:
            obj = cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
            self._assert_access(obj, user)
            self._assert_offering(obj, offering)
            cluster.delete(ResourceKind.KNATIVE_SERVICE, oname)
            return SiteStatus(site=cluster.site, status="Deleted")

        targets = self.deployer.resolve_targets(None)
        statuses = await self.deployer.fanout(targets, remove)
        if all(s.error is not None for s in statuses):
            raise NotFoundError(f"{kind} '{name}' not found")

    async def list_workloads(
        self, kind: str, user: Principal, group: str
    ) -> list[WorkloadSummary]:
        """General info for every workload of this offering owned by `group`.
        Reads only the **local site**: workloads are active/active and identical
        across sites, so one cluster is authoritative and we skip the cross-site
        fan-out. Raises if the local site is unreachable."""
        self.assert_group(user, group)
        offering = kind  # the API kind ("function"/"container") is the offering label
        selector = f"{LABEL_GROUP}={group},{LABEL_OFFERING}={offering}"

        def fetch(cluster: Cluster) -> list[dict]:
            return cluster.get(ResourceKind.KNATIVE_SERVICE, label_selector=selector)

        results = await self.deployer.gather_each([self.deployer.local_cluster()], fetch)
        if all(items is None for _, items in results):
            raise SiteTotalFailure(
                "Listing failed in all sites.",
                details=[{"site": site} for site, _ in results],
            )

        suffix = f"-{group}"
        merged: dict[str, dict] = {}
        for site, items in results:
            if items is None:
                continue
            for obj in items:
                meta = obj.get("metadata", {}) or {}
                oname = meta.get("name", "")
                # object name is "{name}-{group}"; recover the display name
                name = oname[: -len(suffix)] if oname.endswith(suffix) else oname
                annotations = meta.get("annotations", {}) or {}
                status, _ = _ksvc_status(obj)
                entry = merged.setdefault(
                    name, {"host": None, "size": None, "sites": [], "statuses": []}
                )
                entry["host"] = entry["host"] or annotations.get(ANNOTATION_HOST)
                entry["size"] = entry["size"] or annotations.get(ANNOTATION_SIZE)
                entry["sites"].append(site)
                entry["statuses"].append(status)

        summaries = []
        for name in sorted(merged):
            entry = merged[name]
            host = entry["host"] or route_svc.host_for(
                name, group, self.settings.route_domain
            )
            statuses = entry["statuses"]
            if all(s == "Ready" for s in statuses):
                overall = "Ready"
            elif all(s == "Deploying" for s in statuses):
                overall = "Deploying"
            else:
                overall = "Degraded"
            summaries.append(
                WorkloadSummary(
                    name=name,
                    type=kind,  # type: ignore[arg-type]
                    url=f"https://{host}",
                    overallStatus=overall,
                    size=entry["size"],
                    sites=sorted(entry["sites"]),
                )
            )
        return summaries

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
