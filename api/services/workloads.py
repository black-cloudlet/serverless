"""Shared workload engine: build manifests once, fan out to all sites.

Offering-agnostic. FunctionService and ContainerService compose this engine and
add only the offering-specific prep (build-from-Git vs image + pull secret);
apply, host/absence checks, access control and get/delete all live here.
"""

from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from api.auth.claims import Principal
from api.core.config import Settings
from api.models.common import (
    ANNOTATION_GIT_BRANCH,
    ANNOTATION_GIT_PATH,
    ANNOTATION_GIT_URL,
    ANNOTATION_HOST,
    ANNOTATION_RUNTIME,
    ANNOTATION_SIZE,
    LABEL_GROUP,
    LABEL_OFFERING,
    LABEL_WORKLOAD,
    BuildStatusView,
    LogsResponse,
    PodLogs,
    SiteStatus,
    WorkloadResponse,
    WorkloadSummary,
)
from api.models.container import ContainerResponse
from api.models.function import FunctionResponse
from api.services import describe as describe_svc
from api.services import ksvc as ksvc_svc
from api.services import metrics as metrics_svc
from api.services import resources as res
from api.services import route as route_svc
from api.services import secrets as secret_svc
from api.services.deployer import (
    Deployer,
    aggregate,
    overall_status,
    overall_status_for_sites,
    status_code_for,
)
from api.services.env import env_secret_name, resolve_env
from api.services.files import files_name, resolve_files
from common import kpack
from common.cluster import Cluster, ResourceKind
from common.contract import Builder
from common.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
    SiteTotalFailure,
    ValidationError,
)
from common.logging import get_logger
from common.names import object_name, validate_object_name

logger = get_logger(__name__)

OFFERING_FUNCTION = "function"
OFFERING_CONTAINER = "container"

# Israel local time, DST applied from the IANA database. `tzdata` is a
# dependency so this resolves in slim containers with no system zoneinfo.
ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


def _with_build_status(overall: str, build: "BuildStatusView | None") -> str:
    """Fold a function's build state into the KSVC rollup (docs/FUNCTIONS.md).

    The build is checked FIRST: a function whose image does not exist yet is not
    broken, but its KSVC is failing to pull one, which would read as ``Degraded`` for
    a whole normal first build. A failed build is the honest cause of that same
    symptom, so it does report ``Degraded``, with the reason on ``build.message``.

    Args:
        overall: The rollup of the per-site KSVC statuses.
        build: The local site's build status, or None if it has no build.

    Returns:
        The status to report.
    """
    if build is None:
        return overall
    if build.state == "Building":
        return "Building"
    if build.state == "Failed":
        return "Degraded"
    return overall


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
    # A non-string (or missing) timestamp has no valid parse; str keeps the
    # try to just the fromisoformat ValueError (avoids a multi-except tuple).
    if not isinstance(ts, str):
        return None
    try:
        # Kubernetes stamps RFC3339 UTC; present it in Israel local time.
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ISRAEL_TZ)
    except ValueError:
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
    # True = Ready, False = terminal failure, Unknown/absent = progressing.
    # The False/Unknown split is what lets a poller stop instead of spinning.
    state = (ready or {}).get("status")
    if state == "True":
        return "Ready", revision
    if state == "False":
        return "Failed", revision
    return "Deploying", revision


def _ksvc_failure_message(obj: dict) -> str | None:
    """The Knative Ready condition's failure reason/message, when it failed.

    Returns the human-readable ``message`` (falling back to the ``reason`` code)
    of a KSVC's ``Ready`` condition when its status is ``False`` - the rollout
    failure detail (RevisionFailed, image-pull error, ...) to surface as the
    per-site ``error``. None when Ready isn't False or carries no detail.
    """
    conditions = _dig(obj, "status", "conditions", default=[]) or []
    ready = next((c for c in conditions if c.get("type") == "Ready"), None)
    if not ready or ready.get("status") != "False":
        return None
    return ready.get("message") or ready.get("reason") or None


def _revision_replicas(rev: dict | None) -> int | None:
    """The autoscaler's live scale (``Revision.status.actualReplicas``), or None."""
    return _dig(rev, "status", "actualReplicas") if rev else None


def _revision_failure_message(rev: dict | None) -> str | None:
    """The most specific failure detail from a Revision's conditions, if failing.

    A Revision reports the aggregate ``Ready`` condition plus the sub-conditions
    feeding it. A failing sub-condition names the real cause (image pull, crash,
    quota), so it is preferred over the generic aggregate; then any failing
    condition's message, then its reason code.
    """
    conditions = _dig(rev, "status", "conditions", default=[]) or []
    failing = [c for c in conditions if c.get("status") == "False"]
    if not failing:
        return None
    specific = next((c for c in failing if c.get("type") != "Ready" and c.get("message")), None)
    chosen = specific or next((c for c in failing if c.get("message")), None)
    if chosen is not None:
        return chosen.get("message")
    return next((c.get("reason") for c in failing if c.get("reason")), None)


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
            statusUrl=f"/api/v1/groups/{group}/{kind}s/{name}",
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
        self, *, offering: str, group: str, spec, user: Principal, background, work, **extra
    ) -> WorkloadResponse:
        """Run a create's synchronous pre-flight, then schedule the deploy (202).

        Shared by both offerings: validate the spec and check the host is free and the
        name unused, so bad input or a conflict is an immediate 400/403/409/503, then
        queue the deploy and return the Pending 202.

        Args:
            offering: "function" or "container".
            group: The owning group (from the request path).
            spec: The create request (carries name/sites/hostname/env/files).
            user: The authenticated caller.
            background: FastAPI background tasks to schedule the deploy on.
            work: The offering's background create coroutine, run as
                ``work(group, spec, user)``.
            **extra: Offering-specific fields echoed onto the accepted body.

        Returns:
            A Pending response with a ``statusUrl`` to poll.
        """
        self.assert_group(user, group)
        targets = self.deployer.resolve_targets(spec.sites)
        host = self.host_for(spec.name, spec.hostname, group)
        # Validate synchronously (400) before the 202, so bad input never reaches the
        # background deploy.
        self.validate_spec(spec.name, group, user.username, spec.env, spec.files)
        # Host and name in one pass: an immediate 409 is the point of doing this
        # synchronously, and one round trip answers both.
        await self.assert_deployable(spec.name, group, targets, host=host, require_absent=True)
        background.add_task(self.run, work, group, spec, user)
        return self.accepted(offering, spec.name, group, host, **extra)

    async def accept_update(
        self,
        *,
        offering: str,
        group: str,
        name: str,
        spec,
        user: Principal,
        background,
        work,
        pre_check=None,
        **extra,
    ) -> WorkloadResponse:
        """Run an update's synchronous pre-flight, then schedule the deploy (202).

        Loads and authorizes the existing workload, validates the spec, and - since the
        host can change on update - checks the new host is free or already this
        workload's, all synchronously. The loaded state is passed through so the
        background work need not re-fetch it.

        Args:
            offering: "function" or "container".
            group: The owning group (from the request path).
            name: The workload name.
            spec: The update request.
            user: The authenticated caller.
            background: FastAPI background tasks to schedule the deploy on.
            work: The offering's background update coroutine, run as
                ``work(group, name, spec, user, existing)``.
            pre_check: Optional offering-specific ``(spec, existing) -> None``
                validation run against the loaded state, so a check that needs the
                stored state (e.g. a registry-username change) is a synchronous 400.
            **extra: Offering-specific fields echoed onto the accepted body.

        Returns:
            A Pending response with a ``statusUrl`` to poll.
        """
        existing = await self.load_existing(name, offering, user, group)
        if pre_check is not None:
            pre_check(spec, existing)  # offering-specific sync validation (may 4xx)
        # Validate synchronously (400) before the 202, so bad input never reaches the
        # background deploy.
        self.validate_spec(
            name,
            group,
            user.username,
            spec.env,
            spec.files,
            kept_env=existing.get("env_values"),
            kept_files=existing.get("files_values"),
        )
        host = self.host_for(name, spec.hostname, group)
        # A host collision must be a synchronous 409, not a lost background failure.
        # The workload's own mapping counts as available.
        await self.assert_host_available(host, name, group, self.deployer.resolve_targets(None))
        background.add_task(self.run, work, group, name, spec, user, existing)
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
        port: int | None,
        created: bool,
        runtime: str | None = None,
        git_url: str | None = None,
        branch: str | None = None,
        path: str | None = None,
        prev_host: str | None = None,
        kept_env: dict[str, str] | None = None,
        kept_files: dict[str, bytes] | None = None,
        extra_secrets: list[dict] = (),
        local_resources: list[dict] = (),
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
            port: Explicit container port, or None for Knative's default
                (functions pass None). See :func:`api.services.ksvc.build_ksvc`.
            created: True for a create (affects the success status code).
            runtime: Function runtime, stamped as an annotation.
            git_url: Function source repo, stamped as an annotation.
            branch: Function source branch, stamped as an annotation.
            path: Function source sub-directory, stamped as an annotation.
            prev_host: The host the workload currently uses (update only); when it
                differs from the resolved host, the old DomainMapping is retired so
                the old host doesn't stay claimed.
            kept_env: Decoded existing env-Secret values, so a secret env var sent
                without a value keeps its stored value (update only).
            kept_files: Decoded existing files-Secret values, so a secret file sent
                without content keeps its stored content (update only).
            extra_secrets: Owned Secrets applied to every site (the function's
                git token). Not in the managed prune set, so omitting one keeps
                the stored copy.
            local_resources: Owned manifests applied to the local site only (the
                function's Image and build ServiceAccount). Fanning them out
                would race two sites to push the same tag.

        Returns:
            The response body and HTTP status code.
        """
        self.assert_group(user, group)
        oname = object_name(name, group)
        targets = self.deployer.resolve_targets(sites)
        host = self.host_for(name, hostname, group)

        # Re-checked here, immediately before the mutation, and not merely trusted
        # from accept time: this is the guard, and it has to be the last thing that
        # happens before the apply. The DomainMapping name IS the host, so an
        # idempotent apply would otherwise hijack another workload's mapping; on a
        # create the same pass also confirms the name is still unused, which is why
        # the offering services no longer probe for that separately.
        await self.assert_deployable(name, group, targets, host=host, require_absent=created)

        resolved = resolve_files(oname, group, user.username, files, kept_files)
        resolved_env = resolve_env(oname, group, user.username, env, kept_env)
        backing = resolved.backing + resolved_env.backing + list(extra_secrets)
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
            port=port,
            pull_secret=pull_secret_name,
            runtime=runtime,
            git_url=git_url,
            branch=branch,
            path=path,
            ca_config_map=self.settings.ca_bundle.config_map,
            ca_mount_path=self.settings.ca_bundle.mount_path,
            ca_file=self.settings.ca_bundle.file,
        )
        mapping = route_svc.build_domain_mapping(
            name=oname, group=group, owner=user.username, offering=offering, host=host
        )

        # The resolvers emit only what the new spec still needs, so prune the rest:
        # dropping the last secret env var must delete its Secret, not orphan it.
        applied_derived = {
            (ResourceKind.from_kind(m["kind"]), m["metadata"]["name"]) for m in backing
        }
        managed_derived = {
            (ResourceKind.SECRET, env_secret_name(oname)),
            (ResourceKind.CONFIG_MAP, files_name(oname)),
            (ResourceKind.SECRET, files_name(oname)),
        }
        # Keyed on whether the KSVC still references it, not on the manifest: the
        # secret can be carried forward without being re-applied. Containers only.
        if offering == OFFERING_CONTAINER:
            managed_derived.add((ResourceKind.SECRET, secret_svc.pull_secret_name(oname)))
            if pull_secret_name:
                applied_derived.add((ResourceKind.SECRET, pull_secret_name))
        to_prune = () if created else managed_derived - applied_derived

        # Always built on the LOCAL site, whether or not the function runs here.
        # The registry is shared, so a site that only runs it pulls what we pushed.
        build_site = self.deployer.local_site() if local_resources else None
        # A non-target local site gets the build objects only. No KSVC there to own
        # them, so they are applied unowned and delete() reclaims them by name.
        build_only = bool(local_resources) and not any(c.site == build_site for c in targets)
        if build_only:
            await asyncio.to_thread(
                self._apply_build_objects,
                self.deployer.local_cluster(),
                list(extra_secrets) + list(local_resources),
            )

        def apply(cluster: Cluster) -> SiteStatus:
            return self._apply_to_site(
                cluster,
                oname=oname,
                ksvc=ksvc,
                backing=(
                    backing + list(local_resources) if cluster.site == build_site else backing
                ),
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
                **common, runtime=runtime, gitRepo=git_url, branch=branch, path=path
            )
        else:
            body = ContainerResponse(**common, image=image, port=port)
        return body, status_code_for(overall, created=created)

    def _apply_build_objects(self, cluster: Cluster, manifests: list[dict]) -> None:
        """Apply a function's build objects to a site that runs no copy of it.

        Only reached when the local site is excluded from the function's sites.
        The build still belongs here, so the git Secret, build ServiceAccount and
        Image are applied on their own.

        Applied UNOWNED, which is not a choice: an ownerReference must name an owner in
        the same cluster and the KSVC that would be it was never applied here. Nothing
        collects them, so :meth:`delete` removes them by name.

        Args:
            cluster: The local site's cluster client.
            manifests: The git Secret, build ServiceAccount and Image.

        Raises:
            Exception: Any apply error. Failing here means the image would never
                be built, so it is surfaced rather than leaving a function whose
                tag nothing ever pushes.
        """
        for manifest in manifests:
            cluster.apply(manifest)

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

        1. **Prune first**, before anything goes live, so the new spec never runs
           beside a stale Secret/ConfigMap leaking old values.

        2. **KSVC, then owner-stamped backing, then DomainMapping.** The KSVC apply
           returns the UID the ownerReferences need, so nothing is orphaned.

        3. **Roll back a failed create, never a failed update.** A half-applied
           create is deleted so it does not hold the name and host; an update is
           left serving its last-good revision and self-heals on retry.

        4. **Retire the old host last** (update only), so it keeps serving until
           the new mapping is live, and survives a failure above.

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
                pass  # never existed in this site - nothing to prune

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
            # Failed after the KSVC went live. Roll back a create so no half-built
            # workload holds the name; leave an update, which is still serving.
            if created:
                try:
                    cluster.delete(ResourceKind.KNATIVE_SERVICE, oname)
                except Exception:  # noqa: BLE001 - rollback is best-effort
                    logger.exception("rollback of %s failed in %s", oname, cluster.site)
            raise

        # The new mapping is live, so retire the old host's. Best-effort: a leftover
        # only re-claims a host this same workload owns, and is GC'd on delete.
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
        targets = self.deployer.resolve_targets(None)
        found: dict = {}

        def fetch(cluster: Cluster) -> SiteStatus:
            # Only a real 404 means absent; anything else must propagate so a down site
            # is recorded as an error, not mistaken for absence.
            try:
                obj = cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
            except NotFoundError:
                return SiteStatus(site=cluster.site, status="Absent")
            # The KSVC is uniform across sites, so any responder's copy will do;
            # setdefault is atomic under the concurrent fan-out.
            found.setdefault("obj", obj)
            return SiteStatus(site=cluster.site, status="Present")

        statuses = await self.deployer.fanout(targets, fetch)

        obj = found.get("obj")
        if obj is not None:
            labels = (obj.get("metadata", {}) or {}).get("labels", {}) or {}
            # An object_name collision could resolve to another group's workload or
            # the other offering; both mean "not this workload" -> hide as 404.
            if not user.can_access_group(labels.get(LABEL_GROUP, "")) or (
                labels.get(LABEL_OFFERING) != offering
            ):
                raise NotFoundError(f"{offering} workload '{name}' not found")
            # Read the backing Secrets from the local site when it has the workload,
            # else any site that does - they're uniform, so prefer the cheapest hop.
            present = {s.site for s in statuses if s.status == "Present"}
            by_site = {c.site: c for c in targets}
            local = self.deployer.local_site()
            cluster = by_site[local if local in present else next(iter(present))]
            # Reading the backing Secrets is blocking cluster I/O; run it in a thread
            # so it doesn't stall the event loop (as get()/_describe_spec do).
            return await asyncio.to_thread(self._existing_state, obj, cluster, offering, oname)

        # Absent on every site we could reach. If one was unreachable we can't be
        # sure it's truly gone -> fail closed (503), not a misleading 404.
        self._assert_all_sites_checked(statuses, f"load workload '{name}'")
        raise NotFoundError(f"{offering} workload '{name}' not found")

    def _existing_state(self, obj: dict, cluster: Cluster, offering: str, oname: str) -> dict:
        """Read an existing workload's carried-forward state + backing secret values.

        Runs off the event loop (blocking cluster reads). ``env_values``/
        ``files_values`` back the keep-on-write path (fail loud on a transient read;
        see :meth:`_secret_data`). The pull-secret and git reads are best-effort: a
        failure just degrades a registry keep to carrying the existing secret forward.
        """
        ann = (obj.get("metadata", {}) or {}).get("annotations", {}) or {}
        ps_name = describe_svc.pull_secret_name(obj)
        state = {
            "image": _extract_image(obj),
            "runtime": ann.get(ANNOTATION_RUNTIME),
            "gitUrl": ann.get(ANNOTATION_GIT_URL),
            "branch": ann.get(ANNOTATION_GIT_BRANCH),
            "path": ann.get(ANNOTATION_GIT_PATH),
            "host": ann.get(ANNOTATION_HOST),
            "pull_secret": ps_name,
            # Existing secret values, so an update can keep a redacted secret the
            # client sent back without a value (see resolve_env/_files). Env values
            # are text; file content is bytes, which is why they read differently.
            "env_values": self._secret_text(cluster, env_secret_name(oname)),
            "files_values": self._secret_data(cluster, files_name(oname)),
        }
        # Existing registry creds (decoded from the pull secret), so a keep (token
        # omitted) can re-key them to the current image's registry.
        if ps_name:
            try:
                ps = cluster.get(ResourceKind.SECRET, ps_name)
                state["registry_username"] = secret_svc.registry_username(ps)
                state["registry_token"] = secret_svc.registry_token(ps)
            except Exception:  # noqa: BLE001, S110 - best-effort; keep degrades to carry-forward
                pass
        # Functions carry a stored git token; read it so a build-input change can
        # rebuild without the client re-supplying it.
        if offering == OFFERING_FUNCTION:
            git = self._secret_text(cluster, secret_svc.git_secret_name(oname))
            state["git_token"] = git.get(secret_svc.GIT_TOKEN_KEY)
        return state

    def validate_spec(
        self,
        name: str,
        group: str,
        owner: str,
        env,
        files,
        kept_env: dict[str, str] | None = None,
        kept_files: dict[str, bytes] | None = None,
    ) -> None:
        """Validate a spec synchronously, before the request is accepted.

        Runs the in-memory resolution :meth:`apply_workload` will later perform, so bad
        input fails as a 400 at accept time instead of being accepted (202) and dying
        silently in the background deploy.

        Args:
            name: Workload name.
            group: Owning group.
            owner: Username stamped on derived resources.
            env: The submitted env vars.
            files: The submitted file mounts.
            kept_env: Existing env-Secret values a "keep" secret falls back on
                (update only; empty/None on create).
            kept_files: Existing files-Secret values a "keep" secret file falls back
                on (update only; empty/None on create).

        Raises:
            ValidationError: If the env or files cannot be resolved, or if the
                name and group are too long together to be a DNS label.
        """
        try:
            oname = validate_object_name(name, group)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        resolve_files(oname, group, owner, files, kept_files)
        resolve_env(oname, group, owner, env, kept_env)

    async def assert_deployable(
        self,
        name: str,
        group: str,
        targets: list[Cluster],
        *,
        host: str | None = None,
        require_absent: bool = False,
    ) -> None:
        """Assert a workload can be deployed: host free, and optionally name unused.

        Both questions are answered in ONE visit per site. They used to be two
        separate fan-outs, which cost two cross-site round trips per deploy and -
        worse - described two different instants; asking together means a site's
        two answers cannot disagree about the moment they were taken.

        Only a real 404 means free/absent. An unreachable site can't prove either,
        so this fails closed (503) rather than treating silence as consent -
        otherwise a create against a down peer could hijack its DomainMapping or
        overwrite a workload it is still serving.

        This does not make the deploy atomic, and is not meant to: the apply that
        follows is a separate operation, so a peer can still claim the host in
        between. It is the guard that makes that window small and the failure
        loud, which is why the apply path runs it again immediately before
        mutating rather than trusting the accept-time result.

        Args:
            name: The workload name claiming the host.
            group: The workload's owning group.
            targets: The clusters to check.
            host: The external host (== the DomainMapping name); None skips the
                host check (nothing is claiming a host).
            require_absent: Also require that no workload of this name exists
                (create only - an update is expected to find its own).

        Raises:
            ConflictError: If the host belongs to another workload, or the name is
                already taken.
            ServiceUnavailableError: If any site was unreachable.
        """
        oname = object_name(name, group)

        def probe(cluster: Cluster) -> SiteStatus:
            if host is not None:
                try:
                    existing = cluster.get(ResourceKind.DOMAIN_MAPPING, host)
                except NotFoundError:
                    existing = None
                if existing is not None:
                    labels = (existing.get("metadata", {}) or {}).get("labels", {}) or {}
                    # The workload's own mapping counts as available (update path).
                    if labels.get(LABEL_WORKLOAD) != oname:
                        return SiteStatus(site=cluster.site, status="Taken")
            if require_absent:
                try:
                    cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
                    return SiteStatus(site=cluster.site, status="Exists")
                except NotFoundError:
                    pass
            return SiteStatus(site=cluster.site, status="Available")

        statuses = await self.deployer.fanout(targets, probe)
        # The host conflict is reported first: it is the one an idempotent apply
        # would silently resolve by hijacking another workload's mapping.
        if any(s.status == "Taken" for s in statuses):
            raise ConflictError(f"hostname '{host}' is already assigned")
        if any(s.status == "Exists" for s in statuses):
            raise ConflictError(f"workload '{name}' already exists")
        self._assert_all_sites_checked(statuses, f"verify workload '{name}' can be deployed")

    async def assert_host_available(
        self, host: str, name: str, group: str, targets: list[Cluster]
    ) -> None:
        """Assert ``host`` is free (see :meth:`assert_deployable`)."""
        await self.assert_deployable(name, group, targets, host=host)

    async def assert_workload_absent(self, name: str, group: str, targets: list[Cluster]) -> None:
        """Assert no workload named ``{name}-{group}`` exists (see :meth:`assert_deployable`)."""
        await self.assert_deployable(name, group, targets, require_absent=True)

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
        # The spec is uniform across sites, so read it back once from one
        # representative site (local if it has the workload) after the fan-out.
        reps: dict[str, tuple] = {}

        def fetch(cluster: Cluster) -> SiteStatus | None:
            # A 404 means not deployed here, so omit the site rather than fail it.
            # Anything else propagates, keeping a down site visible as Degraded.
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
            # The Revision (for live scale) and pod usage are independent cluster
            # reads; fetch them concurrently to cut this site's read latency.
            with ThreadPoolExecutor(max_workers=2) as pool:
                rev_f = pool.submit(self._revision, cluster, revision)
                usage_f = pool.submit(self._site_usage, cluster, oname)
                rev, usage = rev_f.result(), usage_f.result()
            replicas = _revision_replicas(rev)
            # Prefer the Revision's conditions (the specific cause) over the KSVC's, so
            # a GET explains why it failed instead of a bare status=Failed.
            error = None
            if status == "Failed":
                error = _revision_failure_message(rev) or _ksvc_failure_message(obj)
            return SiteStatus(
                site=cluster.site,
                status=status,
                revision=revision,
                error=error,
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
        # Hidden as a 404 so the response cannot leak that it exists; the real
        # reason is logged, so denied-vs-absent stays debuggable.
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
            build = await asyncio.to_thread(self._build_status, name, group)
            return FunctionResponse(
                **{**common, "overallStatus": _with_build_status(overall, build)},
                runtime=(annotations or {}).get(ANNOTATION_RUNTIME),
                gitRepo=spec.gitRepo if spec else None,
                branch=spec.branch if spec else None,
                path=spec.path if spec else None,
                build=build,
            )
        # container-only: the client-supplied image
        return ContainerResponse(
            **common,
            image=_extract_image(obj) if obj else None,
            registryUsername=spec.registryUsername if spec else None,
            port=spec.port if spec else None,
        )

    def _build_status(self, name: str, group: str) -> BuildStatusView | None:
        """The function's build state from the LOCAL site, or None if it has none.

        One site builds and it is always this one (docs/BUILDING.md), including for a
        function deployed only elsewhere - so the Image is here whenever it
        exists anywhere, and a cross-site fan-out would add latency to every GET
        for something that cannot be found anywhere else.

        Args:
            name: The workload name.
            group: The owning group.

        Returns:
            The build status, or None when there is no Image here or the build
            backend cannot be read - never an error, since a function whose
            image already exists must still report its KSVC status.
        """
        status = self.builder.status(self.deployer.local_cluster(), name, group)
        if status is None:
            return None
        return BuildStatusView(state=status.state, message=status.message)

    def _secret_data(self, cluster: Cluster, name: str) -> dict[str, bytes]:
        """Raw ``data`` of a Secret (base64 -> bytes); ``{}`` if it doesn't exist.

        Bytes, not text: a secret file may hold a keystore or a DER certificate, and
        decoding that to ``str`` here would either raise or produce something that
        cannot be re-encoded when the keep is written back.

        Used by the update path so a redacted "keep" field echoed back is preserved. A
        genuine 404 means no stored values (``{}``). Any other error must NOT be
        swallowed: ``{}`` would make a valid keep look unset and fail it as a 400, so it
        surfaces as a 503 and the update is retried rather than losing a secret.

        Raises:
            ServiceUnavailableError: If the Secret exists but couldn't be read.
        """
        try:
            secret = cluster.get(ResourceKind.SECRET, name)
        except NotFoundError:
            return {}  # no such Secret -> nothing stored
        except Exception as exc:  # noqa: BLE001 - transient/unknown read failure
            raise ServiceUnavailableError(
                f"could not read secret '{name}' to preserve kept values; retry"
            ) from exc
        out: dict[str, bytes] = {}
        for key, val in (secret.get("data") or {}).items():
            try:
                out[key] = base64.b64decode(val)
            except Exception:  # noqa: BLE001, S112 - skip an undecodable key
                continue
        return out

    def _secret_text(self, cluster: Cluster, name: str) -> dict[str, str]:
        """:meth:`_secret_data` as text, for the values that genuinely are text.

        Env values and the git token become a container env var and an HTTP basic-auth
        password, both of which are strings by definition. A stored value that is not
        valid UTF-8 could not have been written through this API, so it is skipped
        rather than guessed at.
        """
        out: dict[str, str] = {}
        for key, raw in self._secret_data(cluster, name).items():
            try:
                out[key] = raw.decode("utf-8")
            except UnicodeDecodeError:  # noqa: S112 - not text, so not an env value
                continue
        return out

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

    def _revision(self, cluster: Cluster, revision: str | None) -> dict | None:
        """Best-effort fetch of the Knative Revision the KSVC points at.

        The Revision carries both the autoscaler's live scale and the specific
        rollout-failure conditions, so a single read feeds both the replica count
        and the per-site error detail.

        Returns:
            The Revision object, or None if it has no revision yet or can't be read.
        """
        if not revision:
            return None
        try:
            return cluster.get(ResourceKind.KNATIVE_REVISION, revision)
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
            NotFoundError: If the workload exists on no site, or the caller may
                not access it (hidden as 404, matching GET).
            ServiceUnavailableError: If any site could not be reached, so the
                delete cannot be confirmed.
        """
        self.assert_group(user, group)
        offering = kind  # the API kind ("function"/"container") is the offering label
        oname = object_name(name, group)
        denied: list[str] = []

        def remove(cluster: Cluster) -> SiteStatus:
            # A clean 404 means "not deployed here", which is not a failure and must
            # not read as one - only a site that cannot answer at all is an error.
            try:
                obj = cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
            except NotFoundError:
                return SiteStatus(site=cluster.site, status="Absent")
            labels = (obj.get("metadata", {}) or {}).get("labels", {}) or {}
            # An object_name collision could resolve to another group's workload or
            # the other offering. Recorded, not raised: raising here would be caught
            # by the fan-out and become indistinguishable from an unreachable site.
            if not user.can_access_group(labels.get(LABEL_GROUP, "")) or (
                labels.get(LABEL_OFFERING) != offering
            ):
                denied.append(cluster.site)
                return SiteStatus(site=cluster.site, status="Denied")
            # Cascades to every owned resource: the config Secrets/ConfigMap, the
            # pull secret, the DomainMapping, the build objects.
            try:
                cluster.delete(ResourceKind.KNATIVE_SERVICE, oname)
            except NotFoundError:
                return SiteStatus(site=cluster.site, status="Absent")  # raced a peer
            return SiteStatus(site=cluster.site, status="Deleted")

        targets = self.deployer.resolve_targets(None)
        statuses = await self.deployer.fanout(targets, remove)

        # An unreachable site cannot confirm the workload is gone. Fail closed (503)
        # so the caller retries, rather than reporting a 404 that reads as "already
        # deleted" while the workload is still serving somewhere (delete is
        # idempotent, so a retry over the sites that did succeed is a no-op).
        self._assert_all_sites_checked(statuses, f"delete {kind} '{name}'")
        if denied:
            logger.debug(
                "delete %s '%s' denied for user %s at %s; hidden as 404",
                kind,
                name,
                user.username,
                ", ".join(sorted(denied)),
            )
            raise NotFoundError(f"{kind} '{name}' not found")

        # Every site answered and none refused, so the workload is gone platform-wide
        # and the build objects can go too. Reached even when nothing was deleted:
        # that is the case where an earlier partial delete orphaned them, and a
        # leftover Image would keep rebuilding a function nothing runs.
        if offering == OFFERING_FUNCTION:
            await asyncio.to_thread(
                self._delete_build_objects, self.deployer.local_cluster(), oname
            )
        if all(s.status == "Absent" for s in statuses):
            raise NotFoundError(f"{kind} '{name}' not found")

    def _delete_build_objects(self, cluster: Cluster, oname: str) -> None:
        """Remove a function's build objects from the local site, by name.

        The build always runs here, and when the function is deployed elsewhere these
        are unowned, so nothing cascades and a leftover Image would keep rebuilding a
        deleted function. When the local site does run it, the KSVC delete already
        cascaded and each call is a no-op 404.

        Best-effort: the KSVC is gone by now either way, and failing the delete
        over a build object would report a workload as undeleted when it is.

        Args:
            cluster: The local site's cluster client.
            oname: The object name (``{name}-{group}``).
        """
        build_name = kpack.build_object_name(oname)
        for kind, obj in (
            (ResourceKind.KPACK_IMAGE, build_name),
            (ResourceKind.SERVICE_ACCOUNT, build_name),
            (ResourceKind.SECRET, secret_svc.git_secret_name(oname)),
        ):
            try:
                cluster.delete(kind, obj)
            except NotFoundError:
                pass  # owned and already cascaded, or never built here
            except Exception:  # noqa: BLE001 - a leftover is logged, not fatal
                logger.exception("could not delete %s '%s' in %s", kind, obj, cluster.site)

    async def logs(
        self,
        kind: str,
        name: str,
        user: Principal,
        group: str,
        *,
        container: str,
        since_seconds: int | None,
        limit_bytes: int | None,
    ) -> LogsResponse:
        """Snapshot the workload's pod logs from the local site only.

        Single-site and point-in-time: reads the running pods on the current
        cluster (Kubernetes keeps no log buffer beyond the node). A workload
        deployed here but scaled to zero returns an empty ``pods`` list.

        Args:
            kind: The offering ("function" or "container").
            name: Workload name.
            user: The authenticated caller.
            group: Owning group.
            container: The pod container to read (e.g. the user-container).
            since_seconds: Only logs newer than this many seconds, if set.
            limit_bytes: Cap on the bytes read per pod, if set.

        Returns:
            The workload's per-pod logs from the local site.

        Raises:
            NotFoundError: If the workload isn't on the local site or the caller
                can't access it (hidden as 404, matching GET).
        """
        self.assert_group(user, group)
        offering = kind  # the API kind ("function"/"container") is the offering label
        oname = object_name(name, group)
        cluster = self.deployer.local_cluster()

        def read() -> list[PodLogs]:
            # Authorize off the KSVC on the local site; a genuine 404 (not
            # deployed here) and a cross-group/offering hit both surface as 404.
            obj = cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
            labels = (obj.get("metadata", {}) or {}).get("labels", {}) or {}
            if not user.can_access_group(labels.get(LABEL_GROUP, "")) or (
                labels.get(LABEL_OFFERING) != offering
            ):
                raise NotFoundError(f"{kind} '{name}' not found")
            pods = cluster.get(
                ResourceKind.POD, label_selector=f"serving.knative.dev/service={oname}"
            )
            out: list[PodLogs] = []
            for pod in pods:
                meta = pod.get("metadata", {}) or {}
                pod_name = meta.get("name", "")
                revision = (meta.get("labels", {}) or {}).get("serving.knative.dev/revision")
                try:
                    text = cluster.pod_logs(
                        pod_name,
                        container=container,
                        since_seconds=since_seconds,
                        limit_bytes=limit_bytes,
                    )
                except NotFoundError:
                    continue  # pod vanished between list and read
                out.append(PodLogs(pod=pod_name, container=container, revision=revision, logs=text))
            return out

        pods = await asyncio.to_thread(read)
        return LogsResponse(
            name=name,
            group=group,
            type=offering,  # type: ignore[arg-type]
            site=self.deployer.local_site(),
            pods=pods,
        )

    async def list(
        self, kind: str, user: Principal, group: str, sort: str = "name"
    ) -> list[WorkloadSummary]:
        """Summarize every workload of this offering owned by ``group``.

        Fans out to all sites and merges best-effort: a workload's ``sites`` lists only
        those that returned it, and its rollup covers just those, so a single-site
        workload reads ``Ready``, not ``Degraded``. An unreachable site is skipped; only
        an all-down fan-out fails the call.

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
            # Same {site, message} shape as aggregate's total-failure; gather_each
            # keeps no per-site error, so message is None.
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
