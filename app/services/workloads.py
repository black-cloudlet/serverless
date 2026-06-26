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
    ServiceUnavailableError,
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
from app.services import resources as res
from app.services import route as route_svc
from app.services import secrets as secret_svc
from app.services.builder import Builder
from app.services.deployer import (
    Deployer,
    aggregate,
    overall_status,
    overall_status_for_sites,
    status_code_for,
)
from app.services.env import env_secret_name, resolve_env
from app.services.files import files_name, resolve_files
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


def _creation_time(obj: dict) -> datetime | None:
    """The workload's creation time from `metadata.creationTimestamp` (RFC3339)."""
    ts = (obj.get("metadata", {}) or {}).get("creationTimestamp")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _ksvc_status(obj: dict) -> tuple[str, str | None]:
    status = obj.get("status", {}) or {}
    conditions = status.get("conditions", []) or []
    ready = next((c for c in conditions if c.get("type") == "Ready"), None)
    revision = status.get("latestReadyRevisionName") or status.get(
        "latestCreatedRevisionName"
    )
    # Knative condition convention: status True = Ready, False = a terminal
    # failure (e.g. RevisionFailed, ProgressDeadlineExceeded, image-pull error),
    # Unknown or absent = still progressing. Distinguishing False from Unknown is
    # what lets a poller stop on a real failure instead of spinning until timeout.
    state = (ready or {}).get("status")
    if state == "True":
        return "Ready", revision
    if state == "False":
        return "Failed", revision
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
            group=group,
            type=kind,
            hostname=host,
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

        # A workload owns a fixed set of derived backing objects; the resolvers
        # only emit a manifest for the ones the new spec still needs. On update,
        # prune the rest so dropping the last secret env var / config file / secret
        # file removes its now-stale Secret/ConfigMap instead of orphaning it
        # (which would otherwise leak old secret values until the workload is
        # deleted). Same name is shared by the files ConfigMap and Secret.
        applied_derived = {
            (
                ResourceKind.SECRET if m["kind"] == "Secret" else ResourceKind.CONFIG_MAP,
                m["metadata"]["name"],
            )
            for m in backing
        }
        managed_derived = {
            (ResourceKind.SECRET, env_secret_name(oname)),
            (ResourceKind.CONFIG_MAP, files_name(oname)),
            (ResourceKind.SECRET, files_name(oname)),
        }
        to_prune = () if created else managed_derived - applied_derived

        def apply(cluster: Cluster) -> SiteStatus:
            # Apply the KSVC first so every derived resource can carry an
            # ownerReference to it (by UID). Kubernetes then garbage-collects them
            # when the KSVC is deleted — including the DomainMapping, whose name is
            # the host (so the host is freed for reuse). The brief window where a
            # fresh revision precedes its env/files Secret/ConfigMap is healed by
            # Knative's reconcile (the kubelet retries the mount).
            applied = cluster.apply(ksvc)
            owner = res.owner_reference(applied[0]) if applied else None
            for manifest in backing:
                cluster.apply(res.with_owner(manifest, owner))
            if pull_secret_manifest:
                cluster.apply(res.with_owner(pull_secret_manifest, owner))
            # DomainMapping exposes the custom host; the Serverless Operator
            # auto-creates the OpenShift Route for it.
            cluster.apply(res.with_owner(mapping, owner))
            # Remove backing objects this update no longer references (best-effort:
            # a NotFound just means it never existed in this site).
            for pkind, pname in to_prune:
                try:
                    cluster.delete(pkind, pname)
                except Exception:  # noqa: BLE001 - best-effort prune; absence is fine
                    logger.debug("prune skipped %s/%s in %s", pkind.kind, pname, cluster.site)
            obj = cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
            status, revision = _ksvc_status(obj)
            return SiteStatus(site=cluster.site, status=status, revision=revision)

        statuses = await self.deployer.fanout(targets, apply)
        overall = aggregate(statuses)
        common = dict(
            name=name,
            group=group,
            type=offering,
            hostname=host,
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
        """Fetch an existing workload (offering-scoped); return {'image','host',...}.

        Raises NotFoundError if it doesn't exist or isn't this offering/group, and
        ServiceUnavailableError if it couldn't be confirmed absent because a site
        was unreachable (so a down site is never reported as a missing workload).
        """
        self.assert_group(user, group)
        oname = object_name(name, group)
        found: dict = {}

        def fetch(cluster: Cluster) -> SiteStatus:
            # Only a genuine 404 means "absent here"; any other error (site down,
            # 5xx) must propagate so fanout records it as a per-site error rather
            # than being mistaken for absence.
            try:
                obj = cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
            except NotFoundError:
                return SiteStatus(site=cluster.site, status="Absent")
            found.setdefault("obj", obj)
            return SiteStatus(site=cluster.site, status="Present")

        statuses = await self.deployer.fanout(self.deployer.resolve_targets(None), fetch)

        obj = found.get("obj")
        if obj is not None:
            labels = (obj.get("metadata", {}) or {}).get("labels", {}) or {}
            # An object_name collision could resolve to another group's workload or
            # the other offering; both mean "not this workload" -> hide as 404.
            if not user.can_access_group(labels.get(LABEL_GROUP, "")) or (
                labels.get(LABEL_OFFERING) != offering
            ):
                raise NotFoundError(f"{offering} workload '{name}' not found")
            ann = (obj.get("metadata", {}) or {}).get("annotations", {}) or {}
            return {
                "image": _extract_image(obj),
                "runtime": ann.get(ANNOTATION_RUNTIME),
                "gitUrl": ann.get(ANNOTATION_GIT_URL),
                "branch": ann.get(ANNOTATION_GIT_BRANCH),
                "pull_secret": describe_svc.pull_secret_name(obj),
            }

        # Absent on every site we could reach. If one was unreachable we can't be
        # sure it's truly gone -> fail closed (503), not a misleading 404.
        self._assert_all_sites_checked(statuses, f"load workload '{name}'")
        raise NotFoundError(f"{offering} workload '{name}' not found")

    def validate_spec(self, name: str, group: str, owner: str, env, files) -> None:
        """Run the pure, in-memory spec resolution that apply_workload will later
        perform, so malformed input (duplicate file mount paths, invalid base64)
        fails synchronously as a 400 at accept time — instead of being accepted
        (202) and then dying silently in the background deploy."""
        oname = object_name(name, group)
        resolve_files(oname, group, owner, files)
        resolve_env(oname, group, owner, env)

    async def assert_host_available(self, host: str, oname: str, targets: list[Cluster]) -> None:
        """Raise ConflictError if `host` is a DomainMapping owned by another workload.

        Only a real 404 means "free"; an unreachable site can't prove the host is
        free, so we fail closed (503) rather than treat it as available — otherwise
        a create against a down peer could hijack its existing DomainMapping.
        """

        def check(cluster: Cluster) -> SiteStatus:
            try:
                existing = cluster.get(ResourceKind.DOMAIN_MAPPING, host)
            except NotFoundError:
                return SiteStatus(site=cluster.site, status="Available")
            labels = (existing.get("metadata", {}) or {}).get("labels", {}) or {}
            owner_workload = labels.get(LABEL_WORKLOAD)
            status = "Available" if owner_workload == oname else "Taken"
            return SiteStatus(site=cluster.site, status=status)

        statuses = await self.deployer.fanout(targets, check)
        if any(s.status == "Taken" for s in statuses):
            raise ConflictError(f"hostname '{host}' is already assigned")
        self._assert_all_sites_checked(statuses, f"verify hostname '{host}' is available")

    async def assert_workload_absent(self, name: str, oname: str, targets: list[Cluster]) -> None:
        """Raise ConflictError if a workload named `oname` already exists (create only).

        Only a real 404 means "absent"; an unreachable site can't prove absence, so
        we fail closed (503) rather than risk creating over an existing workload.
        """

        def probe(cluster: Cluster) -> SiteStatus:
            try:
                cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
                return SiteStatus(site=cluster.site, status="Exists")
            except NotFoundError:
                return SiteStatus(site=cluster.site, status="Absent")

        statuses = await self.deployer.fanout(targets, probe)
        if any(s.status == "Exists" for s in statuses):
            raise ConflictError(f"workload '{name}' already exists")
        self._assert_all_sites_checked(statuses, f"verify workload '{name}' is absent")

    @staticmethod
    def _assert_all_sites_checked(statuses: list[SiteStatus], action: str) -> None:
        """Fail closed if any site could not be reached during a conflict check: a
        missing answer is not evidence of "no conflict"."""
        unreachable = [s.site for s in statuses if s.error is not None]
        if unreachable:
            raise ServiceUnavailableError(
                f"cannot {action}: site(s) unreachable: {', '.join(sorted(unreachable))}"
            )

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

        def fetch(cluster: Cluster) -> SiteStatus | None:
            # A clean 404 means the workload isn't deployed on this site -> omit it
            # from the per-site report (return None) rather than counting it as a
            # failure. Any other error (site down, 5xx) propagates to fanout and is
            # recorded as a per-site error, so a down site stays visible/Degraded.
            try:
                obj = cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
            except NotFoundError:
                return None
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
        results = await self.deployer.fanout(targets, fetch)
        statuses = [s for s in results if s is not None]  # drop sites without it

        if not reps:
            # Present on no reachable site. If a site was unreachable we can't be
            # sure it's absent -> 503; otherwise it's genuinely gone -> 404.
            self._assert_all_sites_checked(statuses, f"get workload '{name}'")
            raise NotFoundError(f"{kind} '{name}' not found")

        # The spec is uniform across sites: read it (and authorize) from the local
        # site if it has the workload, else any site that does.
        obj, cluster = reps.get(self.deployer.local_site()) or next(iter(reps.values()))
        labels = (obj.get("metadata", {}) or {}).get("labels", {}) or {}
        # An object_name collision could resolve to another group/offering; hide as 404.
        if not user.can_access_group(labels.get(LABEL_GROUP, "")) or (
            labels.get(LABEL_OFFERING) != offering
        ):
            raise NotFoundError(f"{kind} '{name}' not found")

        host = meta_holder.get(
            "host", route_svc.host_for(name, group, self.settings.route_domain)
        )
        # A down site counts as Failed (-> Degraded); otherwise the per-site KSVC
        # status drives the rollup, so a workload still coming up reads as Deploying.
        overall = overall_status_for_sites(statuses)
        spec = await asyncio.to_thread(self._describe_spec, cluster, obj)
        common = dict(
            name=name,
            group=group,
            type=kind,
            hostname=host,
            overallStatus=overall,
            size=meta_holder.get("size"),
            createdAt=_creation_time(obj) if obj else None,
            sites=statuses,
            scaling=spec.scaling if spec else None,
            env=spec.env if spec else [],
            files=spec.files if spec else [],
        )
        if kind == OFFERING_FUNCTION:
            # function-only: runtime (from annotation); no image (built artifact)
            annotations = (obj.get("metadata", {}) or {}).get("annotations", {}) if obj else {}
            return FunctionResponse(
                **common,
                runtime=(annotations or {}).get(ANNOTATION_RUNTIME),
                gitRepo=spec.gitRepo if spec else None,
                branch=spec.branch if spec else None,
            )
        # container-only: the client-supplied image
        return ContainerResponse(
            **common,
            image=_extract_image(obj) if obj else None,
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
            # Deleting the KSVC cascades to every derived resource via their
            # ownerReferences (set at apply time): the {workload}-env /
            # {workload}-files Secret & ConfigMap, the imagePullSecret, and the
            # DomainMapping (whose name is the host, freeing it for reuse).
            cluster.delete(ResourceKind.KNATIVE_SERVICE, oname)
            return SiteStatus(site=cluster.site, status="Deleted")

        targets = self.deployer.resolve_targets(None)
        statuses = await self.deployer.fanout(targets, remove)
        if all(s.error is not None for s in statuses):
            raise NotFoundError(f"{kind} '{name}' not found")

    async def list(
        self, kind: str, user: Principal, group: str, sort: str = "name"
    ) -> list[WorkloadSummary]:
        """General info for every workload of this offering owned by `group`,
        sorted by `sort` ("name" or "createdAt"; default name).
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
                    name,
                    {"host": None, "size": None, "createdAt": None, "sites": [], "statuses": []},
                )
                entry["host"] = entry["host"] or annotations.get(ANNOTATION_HOST)
                entry["size"] = entry["size"] or annotations.get(ANNOTATION_SIZE)
                entry["createdAt"] = entry["createdAt"] or _creation_time(obj)
                entry["sites"].append(site)
                entry["statuses"].append(status)

        summaries = []
        for name in merged:
            entry = merged[name]
            host = entry["host"] or route_svc.host_for(
                name, group, self.settings.route_domain
            )
            overall = overall_status(entry["statuses"])
            summaries.append(
                WorkloadSummary(
                    name=name,
                    group=group,
                    type=kind,  # type: ignore[arg-type]
                    hostname=host,
                    overallStatus=overall,
                    size=entry["size"],
                    createdAt=entry["createdAt"],
                    sites=sorted(entry["sites"]),
                )
            )
        if sort == "createdAt":
            _epoch = datetime.min.replace(tzinfo=timezone.utc)  # Nones sort last
            summaries.sort(key=lambda w: (w.createdAt is None, w.createdAt or _epoch))
        else:
            summaries.sort(key=lambda w: w.name)
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
