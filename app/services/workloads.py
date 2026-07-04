"""Shared workload engine: build manifests once, fan out to all sites.

Offering-agnostic. :class:`~app.services.function.FunctionService` and
:class:`~app.services.container.ContainerService` compose this engine and
add only the offering-specific prep (build-from-Git vs image + pull secret);
everything else — apply, host/absence checks, access control, get/delete — lives
here. See docs §3, §4, §6.2.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.auth.claims import Principal
from app.clients.cluster import Cluster, ResourceKind
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
    SiteStatus,
    WorkloadResponse,
    WorkloadSummary,
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

logger = get_logger(__name__)

OFFERING_FUNCTION = "function"
OFFERING_CONTAINER = "container"

# Workload timestamps are surfaced in Israel local time. ZoneInfo reads the IANA
# tz database, so the IDT/IST daylight-saving offset (+03:00 summer, +02:00
# winter) is applied automatically; `tzdata` is a dependency so this resolves in
# slim containers that ship no system zoneinfo.
ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


def object_name(name: str, group: str) -> str:
    """The OpenShift name of a workload and its derived resources: {name}-{group}."""
    return f"{name}-{group}"


def _dig(obj: dict, *path: str, default=None):
    """Walk a nested dict by ``path``, treating a missing/None level as absent.

    Replaces the repeated ``(d.get(k, {}) or {})`` chains used to read Kubernetes
    objects defensively.

    Args:
        obj: The dict to walk.
        path: The successive keys to follow.
        default: Returned if any level is missing or not a dict.

    Returns:
        The nested value, or ``default``.
    """
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _extract_image(obj: dict) -> str | None:
    """The first container image of a KSVC, or None if absent."""
    containers = _dig(obj, "spec", "template", "spec", "containers", default=[]) or []
    return containers[0].get("image") if containers else None


def _creation_time(obj: dict) -> datetime | None:
    """The workload's creation time (`metadata.creationTimestamp`) in Israel time."""
    ts = _dig(obj, "metadata", "creationTimestamp")
    if not ts:
        return None
    try:
        # Kubernetes stamps RFC3339 UTC; present it in Israel local time.
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ISRAEL_TZ)
    except (ValueError, AttributeError):
        return None


def _ksvc_status(obj: dict) -> tuple[str, str | None]:
    """Map a KSVC's Ready condition to a (status, revision) pair.

    Returns:
        ``("Ready"|"Failed"|"Deploying"|"Terminating", revision_name_or_None)``.
    """
    status = _dig(obj, "status", default={}) or {}
    conditions = status.get("conditions", []) or []
    ready = next((c for c in conditions if c.get("type") == "Ready"), None)
    revision = status.get("latestReadyRevisionName") or status.get("latestCreatedRevisionName")
    # A deletionTimestamp means the KSVC is being garbage-collected: report it as
    # Terminating so a GET during the delete window doesn't misreport it as Ready.
    if _dig(obj, "metadata", "deletionTimestamp"):
        return "Terminating", revision
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
        """Initialize the engine.

        Args:
            settings: Global settings.
            deployer: The multi-site fan-out helper.
            builder: The function image builder.
        """
        self.settings = settings
        self.deployer = deployer
        self.builder = builder

    def assert_group(self, user: Principal, group: str) -> None:
        """Reject the request unless the caller may act for ``group``.

        Admins may act for any group. The group is caller-supplied, so this is
        checked on every entry point.

        Args:
            user: The authenticated caller.
            group: The group the request targets.

        Raises:
            ForbiddenError: If ``user`` is not a member of ``group`` (and not an
                admin).
        """
        if not user.can_access_group(group):
            raise ForbiddenError(f"not a member of group '{group}'")

    def host_for(self, name: str, hostname: str | None, group: str) -> str:
        """Resolve the external host for a workload, validating any custom one.

        - no hostname -> the default ``{name}-{group}.{route_domain}``
        - a single label -> the base domain is appended (``{label}.{route_domain}``)
        - an FQDN -> accepted only if it is exactly one label under the base
          domain (``{label}.{route_domain}``); deeper names are rejected

        Args:
            name: The workload name.
            hostname: The caller-supplied custom host, or None for the default.
            group: The owning group.

        Returns:
            The resolved external host.

        Raises:
            ValidationError: If a custom host isn't exactly one label under the
                platform base domain.
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
            raise ValidationError(f"hostname must be exactly one label under '{domain}'")
        return f"{label}.{domain}"

    def accepted(self, kind: str, name: str, group: str, host: str, **extra) -> WorkloadResponse:
        """Build the Pending 202 body returned by accept/accept_update.

        Args:
            kind: The offering ("function" or "container").
            name: Workload name.
            group: Owning group.
            host: The resolved external host.
            **extra: Offering-specific fields echoed back (secrets redacted).

        Returns:
            A response with ``overallStatus="Pending"`` and a ``statusUrl``.
        """
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
        """Run background work, logging (not raising) any failure.

        Failures surface to the client via status polling, not the caller.

        Args:
            fn: The coroutine function to run (e.g. create/update).
            *args: Positional arguments passed to ``fn``.
        """
        try:
            await fn(*args)
        except Exception:  # noqa: BLE001 - background work; surfaced via status polling
            logger.exception("background deploy failed for %s", args)

    async def accept_create(
        self, *, offering: str, spec, user: Principal, background, work, **extra
    ) -> WorkloadResponse:
        """Run a create's synchronous pre-flight, then schedule the deploy (202).

        Shared by both offering services: validate the spec and verify the host is
        free and the name unused (so malformed input or a conflict is an immediate
        400/403/409/503), then queue the offering-specific build+deploy and return
        the Pending 202 body. Only the offering label, the background callable, and
        the echoed fields differ between offerings.

        Args:
            offering: "function" or "container".
            spec: The create request (carries name/group/sites/hostname/env/files).
            user: The authenticated caller.
            background: FastAPI background tasks to schedule the deploy on.
            work: The offering's background create coroutine, run as
                ``work(spec, user)``.
            **extra: Offering-specific fields echoed onto the accepted body.

        Returns:
            A Pending response with a ``statusUrl`` to poll.
        """
        group = spec.group
        self.assert_group(user, group)
        targets = self.deployer.resolve_targets(spec.sites)
        host = self.host_for(spec.name, spec.hostname, group)
        # Surface deploy-time spec validation synchronously (400), before the 202.
        self.validate_spec(spec.name, group, user.username, spec.env, spec.files)
        await self.assert_host_available(host, spec.name, group, targets)
        await self.assert_workload_absent(spec.name, group, targets)
        background.add_task(self.run, work, spec, user)
        return self.accepted(offering, spec.name, group, host, **extra)

    async def accept_update(
        self, *, offering: str, name: str, spec, user: Principal, background, work, **extra
    ) -> WorkloadResponse:
        """Run an update's synchronous pre-flight, then schedule the deploy (202).

        Loads (and authorizes) the existing workload, validates the spec, and —
        since the host can change on update — verifies the (possibly new) host is
        free or already this workload's, all synchronously (immediate
        400/404/409/503). Then queues the offering-specific deploy, passing the
        loaded state through so the background work needn't re-fetch it.

        Args:
            offering: "function" or "container".
            name: The workload name.
            spec: The update request.
            user: The authenticated caller.
            background: FastAPI background tasks to schedule the deploy on.
            work: The offering's background update coroutine, run as
                ``work(name, spec, user, existing)``.
            **extra: Offering-specific fields echoed onto the accepted body.

        Returns:
            A Pending response with a ``statusUrl`` to poll.
        """
        group = spec.group
        existing = await self.load_existing(name, offering, user, group)
        # Surface deploy-time spec validation synchronously (400), before the 202.
        self.validate_spec(name, group, user.username, spec.env, spec.files)
        host = self.host_for(name, spec.hostname, group)
        # The host can change on update; verify it's free (or already ours) now so a
        # collision is a synchronous 409 instead of a silently-swallowed background
        # failure. assert_host_available treats the workload's own mapping as
        # available, so this is a no-op when the host is unchanged.
        await self.assert_host_available(host, name, group, self.deployer.resolve_targets(None))
        background.add_task(self.run, work, name, spec, user, existing)
        return self.accepted(offering, name, group, host, **extra)

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
        prev_host: str | None = None,
    ) -> tuple[WorkloadResponse, int]:
        """Build the manifests once and apply the workload to every target site.

        Applies the KSVC, its derived resources (owned via ownerReferences), and
        the DomainMapping to each site, pruning backing objects the new spec no
        longer references. Offering-agnostic; the function/container services
        supply the offering-specific inputs.

        Args:
            name: Workload name.
            user: The authenticated caller.
            group: Owning group.
            image: The image (or built digest) to deploy.
            offering: "function" or "container".
            env: Env vars to resolve onto the workload.
            files: File mounts to resolve onto the workload.
            scaling: Autoscaling settings.
            size: Resource t-shirt size.
            hostname: Optional custom host.
            sites: Target site names, or None for all.
            pull_secret_name: Name of the image pull secret, if any.
            pull_secret_manifest: The pull secret manifest to apply, if any.
            created: True for a create (affects the success status code).
            runtime: Function runtime, stamped as an annotation.
            git_url: Function source repo, stamped as an annotation.
            branch: Function source branch, stamped as an annotation.
            prev_host: The host the workload currently uses (update only); when it
                differs from the resolved host, the old DomainMapping is retired so
                the old host doesn't stay claimed.

        Returns:
            The response body and HTTP status code.
        """
        self.assert_group(user, group)
        oname = object_name(name, group)
        targets = self.deployer.resolve_targets(sites)
        host = self.host_for(name, hostname, group)

        # The DomainMapping name IS the host, so an idempotent apply would hijack
        # another workload's mapping. Reject a host already owned by someone else.
        await self.assert_host_available(host, name, group, targets)

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
            (ResourceKind.from_kind(m["kind"]), m["metadata"]["name"]) for m in backing
        }
        managed_derived = {
            (ResourceKind.SECRET, env_secret_name(oname)),
            (ResourceKind.CONFIG_MAP, files_name(oname)),
            (ResourceKind.SECRET, files_name(oname)),
        }
        to_prune = () if created else managed_derived - applied_derived

        def apply(cluster: Cluster) -> SiteStatus:
            return self._apply_to_site(
                cluster,
                oname=oname,
                ksvc=ksvc,
                backing=backing,
                pull_secret_manifest=pull_secret_manifest,
                mapping=mapping,
                to_prune=to_prune,
                created=created,
                prev_host=prev_host,
            )

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
            createdAt=datetime.now(ISRAEL_TZ) if created else None,
        )
        if offering == OFFERING_FUNCTION:
            body: WorkloadResponse = FunctionResponse(
                **common, runtime=runtime, gitRepo=git_url, branch=branch
            )
        else:
            body = ContainerResponse(**common, image=image)
        return body, status_code_for(overall, created=created)

    def _apply_to_site(
        self,
        cluster: Cluster,
        *,
        oname: str,
        ksvc: dict,
        backing: list[dict],
        pull_secret_manifest: dict | None,
        mapping: dict,
        to_prune,
        created: bool,
        prev_host: str | None = None,
    ) -> SiteStatus:
        """Apply one workload to a single site, fail-closed (runs in a thread).

        Order matters for the no-stale-secret guarantee:

        1. **Prune first.** Delete the backing objects the new spec no longer
           references *before* anything goes live. A 404 means it never existed
           here (fine); any other error is raised — aborting this site's update
           (reported as ``Failed`` by the fan-out) rather than letting the new
           spec go live alongside a stale, now-unreferenced Secret/ConfigMap that
           would leak old secret values. ``to_prune`` is empty on create.
        2. **KSVC, then owner-stamped backing, then DomainMapping.** Applying the
           KSVC first yields its UID for the ownerReferences, so every derived
           resource is GC'd when the KSVC is deleted (including the DomainMapping,
           whose name is the host, freeing it for reuse). Stamping the owner ref
           on backing immediately after keeps them from ever being orphaned (an
           orphan would itself leak secret data).
        3. **Roll back a failed create, never a failed update.** If a backing /
           pull-secret / mapping apply fails *after* the KSVC was applied, the KSVC
           briefly references a Secret/ConfigMap that isn't there. On a **create**
           we delete the KSVC (best-effort; cascades to anything already created)
           so a partial workload isn't left occupying the name + host. On an
           **update** we must NOT delete: the KSVC is serving live traffic, and
           Knative keeps routing to the last-good revision (a new revision that
           can't mount its Secret never becomes Ready), so the failure self-heals
           on retry without taking the workload down or releasing its host.
        4. **Retire the old host last (update only).** If the host changed, the
           new DomainMapping is now live; only then delete the old host's mapping
           (whose name *is* the old host). Pruning it *last* — not via
           ``to_prune``, which prunes first — keeps the old host serving until the
           new one is in place (no custom-host gap) and leaves it intact if an
           apply above failed. Best-effort: a leftover old mapping only re-claims a
           host this same workload owns, and is GC'd on delete.

        Args:
            cluster: The target site's cluster client.
            oname: The object name (``{name}-{group}``).
            ksvc: The Knative Service manifest.
            backing: The derived backing manifests (env/files Secret/ConfigMap).
            pull_secret_manifest: The image-pull Secret manifest, if any.
            mapping: The DomainMapping manifest.
            to_prune: ``(ResourceKind, name)`` pairs to remove first.
            created: True for a create (enables rollback of the new KSVC on a
                mid-apply failure); False for an update (no destructive rollback).
            prev_host: The host the workload currently uses; when it differs from
                this apply's host, the old DomainMapping is retired after the new
                one is live (update only).

        Returns:
            The per-site status.

        Raises:
            Exception: Any non-404 prune/apply error, surfaced as a per-site
                failure by the fan-out.
        """
        for pkind, pname in to_prune:
            try:
                cluster.delete(pkind, pname)
            except NotFoundError:
                pass  # never existed in this site — nothing to prune

        applied = cluster.apply(ksvc)
        owner = res.owner_reference(applied[0]) if applied else None
        try:
            for manifest in backing:
                cluster.apply(res.with_owner(manifest, owner))
            if pull_secret_manifest:
                cluster.apply(res.with_owner(pull_secret_manifest, owner))
            # DomainMapping exposes the custom host; the Serverless Operator
            # auto-creates the OpenShift Route for it.
            cluster.apply(res.with_owner(mapping, owner))
        except Exception:
            # Backing/mapping apply failed after the KSVC went live. On a create,
            # roll the KSVC back (best-effort; cascades to any derived object via
            # ownerReferences) so no half-built workload lingers on the name/host;
            # on an update, leave it — Knative keeps serving the last-good revision.
            if created:
                try:
                    cluster.delete(ResourceKind.KNATIVE_SERVICE, oname)
                except Exception:  # noqa: BLE001 - rollback is best-effort
                    logger.exception("rollback of %s failed in %s", oname, cluster.site)
            raise

        # The host changed on this update: the new DomainMapping is live now, so
        # retire the old host's mapping (its name == the old host). Best-effort —
        # a 404 means it was never here; any other error is logged but not fatal,
        # since the new host already works and a stale old mapping only re-claims a
        # host this same workload owns (and is GC'd on delete).
        new_host = mapping["metadata"]["name"]
        if not created and prev_host and prev_host != new_host:
            try:
                cluster.delete(ResourceKind.DOMAIN_MAPPING, prev_host)
            except NotFoundError:
                pass
            except Exception:  # noqa: BLE001 - old-host cleanup is best-effort
                logger.exception(
                    "retiring old host %s for %s failed in %s",
                    prev_host,
                    oname,
                    cluster.site,
                )

        obj = cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
        status, revision = _ksvc_status(obj)
        return SiteStatus(site=cluster.site, status=status, revision=revision)

    async def load_existing(self, name: str, offering: str, user: Principal, group: str) -> dict:
        """Fetch an existing workload's carried-forward state (offering-scoped).

        Reads from whichever site has the workload; a down site is never reported
        as a missing workload.

        Args:
            name: The workload name.
            offering: The expected offering ("function" or "container").
            user: The authenticated caller.
            group: The owning group.

        Returns:
            A dict with the image and carried-forward build/pull metadata.

        Raises:
            NotFoundError: If it doesn't exist or isn't this offering/group.
            ServiceUnavailableError: If it couldn't be confirmed absent because a
                site was unreachable.
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
                "host": ann.get(ANNOTATION_HOST),
                "pull_secret": describe_svc.pull_secret_name(obj),
            }

        # Absent on every site we could reach. If one was unreachable we can't be
        # sure it's truly gone -> fail closed (503), not a misleading 404.
        self._assert_all_sites_checked(statuses, f"load workload '{name}'")
        raise NotFoundError(f"{offering} workload '{name}' not found")

    def validate_spec(self, name: str, group: str, owner: str, env, files) -> None:
        """Validate a spec synchronously, before the request is accepted.

        Runs the pure, in-memory resolution that :meth:`apply_workload` will later
        perform, so malformed input (duplicate file mount paths, invalid base64)
        fails as a 400 at accept time instead of being accepted (202) and then
        dying silently in the background deploy.

        Args:
            name: Workload name.
            group: Owning group.
            owner: Username stamped on derived resources.
            env: The submitted env vars.
            files: The submitted file mounts.

        Raises:
            ValidationError: If the env or files cannot be resolved.
        """
        oname = object_name(name, group)
        resolve_files(oname, group, owner, files)
        resolve_env(oname, group, owner, env)

    async def assert_host_available(
        self, host: str, name: str, group: str, targets: list[Cluster]
    ) -> None:
        """Assert ``host`` is free, failing closed if a site is unreachable.

        Only a real 404 means "free"; an unreachable site can't prove the host is
        free, so we fail closed (503) rather than treat it as available — otherwise
        a create against a down peer could hijack its existing DomainMapping.

        Args:
            host: The external host (== the DomainMapping name) to check.
            name: The workload name claiming the host.
            group: The workload's owning group.
            targets: The clusters to check.

        Raises:
            ConflictError: If the host is a DomainMapping owned by another workload.
            ServiceUnavailableError: If any site was unreachable.
        """
        oname = object_name(name, group)

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

    async def assert_workload_absent(self, name: str, group: str, targets: list[Cluster]) -> None:
        """Assert no workload named ``{name}-{group}`` exists yet (create only).

        Only a real 404 means "absent"; an unreachable site can't prove absence, so
        we fail closed (503) rather than risk creating over an existing workload.

        Args:
            name: The workload name (also used in the error message).
            group: The workload's owning group.
            targets: The clusters to check.

        Raises:
            ConflictError: If a workload named ``{name}-{group}`` already exists.
            ServiceUnavailableError: If any site was unreachable.
        """
        oname = object_name(name, group)

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
        """Fail closed if any site could not be reached during a conflict check.

        A missing answer is not evidence of "no conflict".

        Args:
            statuses: The per-site results of the conflict check.
            action: Human phrase describing the check, for the error message.

        Raises:
            ServiceUnavailableError: If any site reported an error.
        """
        unreachable = [s.site for s in statuses if s.error is not None]
        if unreachable:
            raise ServiceUnavailableError(
                f"cannot {action}: site(s) unreachable: {', '.join(sorted(unreachable))}"
            )

    async def get(self, kind: str, name: str, user: Principal, group: str) -> WorkloadResponse:
        """Read one workload with live per-site status and its redacted spec.

        Fans out to all sites; a site that returns a clean 404 is omitted, while
        an unreachable site stays visible and degrades the rollup.

        Args:
            kind: The offering ("function" or "container").
            name: Workload name.
            user: The authenticated caller.
            group: Owning group.

        Returns:
            The full single-workload response.

        Raises:
            NotFoundError: If the workload exists on no reachable site.
            ServiceUnavailableError: If it can't be confirmed absent because a
                site was unreachable.
        """
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
            # Replica count and live usage are two independent cluster reads;
            # fetch them concurrently to cut this site's read latency.
            with ThreadPoolExecutor(max_workers=2) as pool:
                replicas_f = pool.submit(self._site_replicas, cluster, revision)
                usage_f = pool.submit(self._site_usage, cluster, oname)
                replicas, usage = replicas_f.result(), usage_f.result()
            return SiteStatus(
                site=cluster.site,
                status=status,
                revision=revision,
                replicas=replicas,
                usage=usage,
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
        # An object_name collision could resolve to another group/offering; hide as
        # 404 (privacy-preserving — don't leak that it exists). Log the real reason
        # server-side so denied-vs-absent is still debuggable from the logs.
        if not user.can_access_group(labels.get(LABEL_GROUP, "")) or (
            labels.get(LABEL_OFFERING) != offering
        ):
            logger.debug(
                "get %s '%s' denied for user %s (group=%s, offering=%s); hidden as 404",
                kind,
                name,
                user.username,
                labels.get(LABEL_GROUP),
                labels.get(LABEL_OFFERING),
            )
            raise NotFoundError(f"{kind} '{name}' not found")

        host = meta_holder.get("host", route_svc.host_for(name, group, self.settings.route_domain))
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
        """Read the desired-state spec (secrets redacted) from a KSVC.

        Fetches the file ConfigMap(s) for non-secret file contents and the pull
        secret for the registry username (never the token). Best-effort: a failed
        read just leaves the corresponding field null.
        """
        configmaps: dict[str, dict] = {}
        for cm_name in describe_svc.configmap_refs(obj):
            try:
                cm = cluster.get(ResourceKind.CONFIG_MAP, cm_name)
                configmaps[cm_name] = cm.get("data") or {}
            except Exception:  # noqa: BLE001, S110 - content is best-effort, skip silently
                pass
        registry_username = None
        ps_name = describe_svc.pull_secret_name(obj)
        if ps_name:
            try:
                secret = cluster.get(ResourceKind.SECRET, ps_name)
                registry_username = secret_svc.registry_username(secret)
            except Exception:  # noqa: BLE001, S110 - username is best-effort, skip silently
                pass
        return describe_svc.parse_spec(obj, configmaps, registry_username=registry_username)

    def _site_replicas(self, cluster: Cluster, revision: str | None) -> int | None:
        """Best-effort running pod count from the revision the KSVC points at.

        Uses the autoscaler's authoritative scale
        (``Revision.status.actualReplicas``), which doesn't depend on the metrics
        API.

        Returns:
            The replica count, or None if it can't be read.
        """
        if not revision:
            return None
        try:
            rev = cluster.get(ResourceKind.KNATIVE_REVISION, revision)
            return (rev.get("status", {}) or {}).get("actualReplicas")
        except Exception:  # noqa: BLE001 - best-effort, never fatal
            return None

    def _site_usage(self, cluster: Cluster, oname: str):
        """Best-effort live cpu/memory summed over the workload's running pods.

        Returns:
            The usage summary, or None if the metrics API is unavailable or the
            workload is scaled to zero (no running pods).
        """
        try:
            items = cluster.get(
                ResourceKind.POD_METRICS,
                label_selector=f"serving.knative.dev/service={oname}",
            )
            return metrics_svc.sum_usage(items)
        except Exception:  # noqa: BLE001 - usage is best-effort, never fatal
            return None

    async def delete(self, kind: str, name: str, user: Principal, group: str) -> None:
        """Delete a workload from every site; GC cascades its derived resources.

        Args:
            kind: The offering ("function" or "container").
            name: Workload name.
            user: The authenticated caller.
            group: Owning group.

        Raises:
            NotFoundError: If the workload exists on no site.
        """
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
        """Summarize every workload of this offering owned by ``group``.

        Fans out to **all sites** and merges the results best-effort: each
        workload's ``sites`` lists only the sites that returned it, and its rollup
        status is computed over just those sites (so a workload deployed to a
        single site reads ``Ready``, not ``Degraded``). A site that is unreachable
        is skipped rather than failing the whole list; only when *every* site is
        down is the call failed.

        Args:
            kind: The offering ("function" or "container").
            user: The authenticated caller.
            group: The owning group.
            sort: Sort key, "name" or "createdAt" (default "name").

        Returns:
            The per-workload summaries.

        Raises:
            SiteTotalFailure: If every site is unreachable.
        """
        self.assert_group(user, group)
        offering = kind  # the API kind ("function"/"container") is the offering label
        selector = f"{LABEL_GROUP}={group},{LABEL_OFFERING}={offering}"

        def fetch(cluster: Cluster) -> list[dict]:
            return cluster.get(ResourceKind.KNATIVE_SERVICE, label_selector=selector)

        results = await self.deployer.gather_each(self.deployer.resolve_targets(None), fetch)
        if all(items is None for _, items in results):
            # Same details shape as deployer.aggregate's total-failure ({site,
            # message}); gather_each doesn't retain the per-site error, so message
            # is None.
            raise SiteTotalFailure(
                "Listing failed in all sites.",
                details=[{"site": site, "message": None} for site, _ in results],
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
        for name, entry in merged.items():
            host = entry["host"] or route_svc.host_for(name, group, self.settings.route_domain)
            overall = overall_status(entry["statuses"])
            summaries.append(
                WorkloadSummary(
                    name=name,
                    group=group,
                    type=kind,
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
        """Ensure the caller may access the object's group.

        Raises:
            ForbiddenError: If the caller can't access the resource's group.
        """
        labels = (obj.get("metadata", {}) or {}).get("labels", {}) or {}
        group = labels.get(LABEL_GROUP, "")
        if not user.can_access_group(group):
            raise ForbiddenError("not permitted for this resource's group")

    def _assert_offering(self, obj: dict, offering: str) -> None:
        """Ensure the object is the expected offering.

        Prevents /functions acting on a container of the same name (and vice
        versa).

        Raises:
            NotFoundError: If the object's offering label doesn't match.
        """
        labels = (obj.get("metadata", {}) or {}).get("labels", {}) or {}
        if labels.get(LABEL_OFFERING) != offering:
            raise NotFoundError("workload not found")
