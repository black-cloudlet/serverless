"""Shared workload engine: build manifests once, fan out to all sites.

Offering-agnostic. FunctionService and ContainerService compose this engine and
add only the offering-specific prep (build-from-Git vs image + pull secret);
apply, host/absence checks, access control and get/delete all live here.

What lives here is the *orchestration* - which sites to visit, in what order,
and what a partial answer means. The pieces it orchestrates were pulled out to
be readable on their own, and are worth knowing before reading this file:

* :mod:`api.services.ksvc_state`  - interpret a Knative object (pure, no I/O)
* :mod:`api.services.preflight`   - the guards that run before any write
* :mod:`api.services.site_apply`  - write one workload into one site
* :mod:`api.services.site_read`   - read one workload's state back out

The ``assert_*``/``host_for``/``validate_spec`` methods below are thin
delegations to :mod:`api.services.preflight`, kept on the engine because that is
the object the offering services and the routers hold.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from api.auth.claims import Principal
from api.core.config import Settings
from api.models.common import (
    ANNOTATION_HOST,
    ANNOTATION_RUNTIME,
    ANNOTATION_RUNTIME_VERSION,
    ANNOTATION_SIZE,
    LABEL_GROUP,
    LABEL_OFFERING,
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
from api.services import ksvc_state, preflight, site_apply, site_read
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
from api.services.ksvc_state import ISRAEL_TZ, ksvc_failure_message, revision_failure_message
from common.build import BuildBackend
from common.cluster import Cluster, ResourceKind
from common.errors import (
    ForbiddenError,
    NotFoundError,
    SiteTotalFailure,
)
from common.labels import OFFERING_CONTAINER, OFFERING_FUNCTION
from common.logging import get_logger
from common.names import object_name

logger = get_logger(__name__)


class WorkloadService:
    """Offering-agnostic orchestration shared by the function/container services."""

    def __init__(self, settings: Settings, deployer: Deployer, builder: BuildBackend):
        """Initialize the engine.

        Args:
            settings: Global settings.
            deployer: The multi-site fan-out helper.
            builder: The function image build backend.
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
        """Resolve the external host, validating any custom one.

        See :func:`api.services.preflight.resolve_host`.
        """
        return preflight.resolve_host(name, hostname, group, self.settings.route_domain)

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

        See :func:`api.services.preflight.validate_spec`.
        """
        preflight.validate_spec(name, group, owner, env, files, kept_env, kept_files)

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

        See :func:`api.services.preflight.assert_deployable`.
        """
        await preflight.assert_deployable(
            self.deployer, name, group, targets, host=host, require_absent=require_absent
        )

    async def assert_host_available(
        self, host: str, name: str, group: str, targets: list[Cluster]
    ) -> None:
        """Assert ``host`` is free (see :meth:`assert_deployable`)."""
        await self.assert_deployable(name, group, targets, host=host)

    async def assert_workload_absent(self, name: str, group: str, targets: list[Cluster]) -> None:
        """Assert no workload named ``{name}-{group}`` exists (see :meth:`assert_deployable`)."""
        await self.assert_deployable(name, group, targets, require_absent=True)

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
        version: str | None = None,
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
            version: Requested language version, stamped as an annotation. None
                means the caller took the platform default.
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
            version=version,
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
                site_apply.apply_build_objects,
                self.deployer.local_cluster(),
                list(extra_secrets) + list(local_resources),
            )

        def apply(cluster: Cluster) -> SiteStatus:
            return site_apply.apply_to_site(
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
                **common,
                runtime=runtime,
                version=version,
                gitRepo=git_url,
                branch=branch,
                path=path,
            )
        else:
            body = ContainerResponse(**common, image=image, port=port)
        return body, status_code_for(overall, created=created)

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
            # so it doesn't stall the event loop (as get()/describe_spec do).
            return await asyncio.to_thread(site_read.existing_state, obj, cluster, offering, oname)

        # Absent on every site we could reach. If one was unreachable we can't be
        # sure it's truly gone -> fail closed (503), not a misleading 404.
        preflight.assert_all_sites_checked(statuses, f"load workload '{name}'")
        raise NotFoundError(f"{offering} workload '{name}' not found")

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
            status, revision = ksvc_state.ksvc_status(obj)
            # Sequential on purpose. These two reads used to run in a
            # ThreadPoolExecutor built per site per request, which spawned and tore
            # down two threads on every poll - and nested that pool inside the
            # default executor this very function borrowed its worker from, so
            # enough concurrent polls filled the outer pool with workers doing
            # nothing but waiting on the inner one. Concurrency belongs at the
            # fan-out, where sites already run in parallel; both of these go to the
            # same cluster, so running them in order costs one round trip.
            rev = site_read.revision(cluster, revision)
            usage = site_read.site_usage(cluster, oname)
            replicas = ksvc_state.revision_replicas(rev)
            # Prefer the Revision's conditions (the specific cause) over the KSVC's, so
            # a GET explains why it failed instead of a bare status=Failed.
            error = None
            if status == "Failed":
                error = revision_failure_message(rev) or ksvc_failure_message(obj)
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
            preflight.assert_all_sites_checked(statuses, f"get workload '{name}'")
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
        # Independent reads of different objects - the spec's ConfigMaps and pull
        # secret, and the function's kpack Image - so they overlap instead of
        # chaining two round trips onto the response. Only a function has a build.
        spec_read = asyncio.to_thread(site_read.describe_spec, cluster, obj)
        if kind == OFFERING_FUNCTION:
            spec, build = await asyncio.gather(
                spec_read, asyncio.to_thread(self._build_status, name, group)
            )
        else:
            spec, build = await spec_read, None
        # Neither `obj` nor `spec` is optional from here: `reps` is non-empty
        # (guarded above) and every entry holds an object, and describe_spec
        # always returns a WorkloadSpec. Guarding them would advertise a nullable
        # that does not exist, which is how a real one stops being noticeable.
        common = dict(
            name=name,
            group=group,
            type=kind,
            hostname=host,
            overallStatus=overall,
            size=meta_holder.get("size"),
            createdAt=ksvc_state.creation_time(obj),
            sites=statuses,
            scaling=spec.scaling,
            env=spec.env,
            files=spec.files,
        )
        if kind == OFFERING_FUNCTION:
            # function-only: runtime (from annotation); no image (built artifact)
            annotations = (obj.get("metadata", {}) or {}).get("annotations", {}) or {}
            return FunctionResponse(
                **{**common, "overallStatus": ksvc_state.with_build_status(overall, build)},
                runtime=annotations.get(ANNOTATION_RUNTIME),
                version=annotations.get(ANNOTATION_RUNTIME_VERSION),
                gitRepo=spec.gitRepo,
                branch=spec.branch,
                path=spec.path,
                build=build,
            )
        # container-only: the client-supplied image
        return ContainerResponse(
            **common,
            image=ksvc_state.extract_image(obj),
            registryUsername=spec.registryUsername,
            port=spec.port,
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
        preflight.assert_all_sites_checked(statuses, f"delete {kind} '{name}'")
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
                site_apply.delete_build_objects, self.deployer.local_cluster(), oname
            )
        if all(s.status == "Absent" for s in statuses):
            raise NotFoundError(f"{kind} '{name}' not found")

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
                status, _ = ksvc_state.ksvc_status(obj)
                entry = merged.setdefault(
                    name,
                    {"host": None, "size": None, "createdAt": None, "sites": [], "statuses": []},
                )
                entry["host"] = entry["host"] or annotations.get(ANNOTATION_HOST)
                entry["size"] = entry["size"] or annotations.get(ANNOTATION_SIZE)
                entry["createdAt"] = entry["createdAt"] or ksvc_state.creation_time(obj)
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
