"""Shared workload engine: build manifests once, fan out to all regions.

Offering-agnostic. FunctionService and ContainerService compose this engine and
add only the offering-specific prep (build-from-Git vs image + pull secret);
apply, host/absence checks, access control and get/delete all live here.

What lives here is the *orchestration* - which regions to visit, in what order,
and what a partial answer means. Everything it orchestrates was pulled out to be
readable and testable on its own:

* :mod:`api.services.manifests` - build what gets applied (pure)
* :mod:`api.services.regions`     - fan out, and write/read one region
* :mod:`api.services.state`     - interpret what came back (pure)
* :mod:`api.services.builder`   - the function image build

The ``assert_*``/``host_for``/``validate_spec`` methods below are thin
delegations to :mod:`api.services.regions.preflight`, kept on the engine because
that is the object the offering services and the routers hold.
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass, field
from datetime import datetime

from cloudlet_apis.auth import Principal
from cloudlet_apis.logging import get_logger

from api.core.config import Settings
from api.core.paths import api_base
from api.models.common import (
    ANNOTATION_HOST,
    ANNOTATION_PULL_STAMP,
    ANNOTATION_SIZE,
    LABEL_GROUP,
    LABEL_OFFERING,
    BuildStatusView,
    LogLine,
    PodLogSnapshot,
    PodLogStreamOpen,
    PodRoster,
    RegionStats,
    RegionStatus,
    WorkloadResponse,
    WorkloadStatsResponse,
    WorkloadSummary,
)
from api.services.builder import registry as registry_svc
from api.services.manifests import ksvc as ksvc_svc
from api.services.manifests import route as route_svc
from api.services.manifests.env import env_secret_name, resolve_env
from api.services.manifests.files import files_name, resolve_files
from api.services.offering import DeleteContext, Offering
from api.services.regions import preflight, region_apply, region_read
from api.services.regions.deployer import (
    Deployer,
    aggregate,
    overall_status_for_regions,
    status_code_for,
)
from api.services.state import describe as describe_svc
from api.services.state import ksvc_state, ownership
from api.services.state import metrics as metrics_svc
from api.services.state import summaries as summaries_svc
from api.services.state.ksvc_state import ISRAEL_TZ, ksvc_failure_message, revision_failure_message
from api.services.streams import logs as logs_stream
from api.services.streams import pods as pods_stream
from api.services.streams import stats as stats_stream
from api.services.streams.capacity import StreamCapacity, StreamSlot
from api.services.streams.sse import StreamEvent
from common.build import BuildBackend, BuildPlan
from common.cluster import Cluster, ResourceKind
from common.config import RegistryConfig
from common.errors import (
    ForbiddenError,
    NotFoundError,
    RegionTotalFailure,
)
from common.names import object_name

logger = get_logger(__name__)


class _SlotGuardedStream:
    """``inner``'s events, with ``slot`` released however the stream ends.

    The slot is held for the life of this object, not of the call that admitted
    it - which is why it cannot simply be a ``with``. An object rather than a
    wrapping generator, deliberately: closing a *never-started* generator skips
    its body, so a ``finally`` in one cannot cover the stream that is handed to
    the response layer and then never iterated (a client that disconnects
    before the body begins) - exactly the stream whose slot would otherwise be
    gone until a restart. Here ``aclose`` always runs, exhaustion and failure
    release directly, and the ``weakref.finalize`` backstop covers an object
    the response layer dropped without closing. Release is idempotent, so the
    several owners cannot double-free.
    """

    def __init__(self, slot: StreamSlot, inner: AsyncGenerator[StreamEvent, None]):
        self._slot = slot
        self._inner = inner
        # Bound to the slot only - a reference to `self` here would keep this
        # object alive forever and the finalizer from ever firing.
        weakref.finalize(self, slot.release)

    def __aiter__(self) -> _SlotGuardedStream:
        return self

    async def __anext__(self) -> StreamEvent:
        try:
            return await self._inner.__anext__()
        except BaseException:
            # StopAsyncIteration included: however the stream ends, the slot
            # goes back now, not when the caller remembers to aclose.
            self._slot.release()
            raise

    async def aclose(self) -> None:
        try:
            await self._inner.aclose()
        finally:
            self._slot.release()


def _slot_guarded(slot: StreamSlot, inner: AsyncGenerator[StreamEvent, None]) -> _SlotGuardedStream:
    """Wrap ``inner`` so ``slot`` is released however the stream ends."""
    return _SlotGuardedStream(slot, inner)


def _hidden_404(action: str, kind: str, name: str, user: Principal, obj: dict) -> NotFoundError:
    """Log a denied read and return the 404 that hides it.

    Denied and absent are the same answer to the caller, so the response cannot
    leak that the workload exists; the real reason goes to the log, where
    denied-vs-absent stays debuggable.
    """
    labels = (obj.get("metadata", {}) or {}).get("labels", {}) or {}
    logger.debug(
        "%s %s '%s' denied for user %s (group=%s, offering=%s); hidden as 404",
        action,
        kind,
        name,
        user.username,
        labels.get(LABEL_GROUP),
        labels.get(LABEL_OFFERING),
    )
    return NotFoundError(f"{kind} '{name}' not found")


@dataclass
class ApplyRequest:
    """Everything one apply needs, as a value instead of a signature.

    :meth:`WorkloadService.apply_workload` took twenty-five keyword arguments, and
    the reason it could is that they were the *union* of both offerings' needs -
    a container passed five dead build-metadata nulls, a function
    passed a dead pull-secret manifest. Bundling them does not by itself fix
    that, but it puts the whole input in one place where the offering-specific
    tail is visible as a group rather than as more parameters.

    Attributes:
        name: Workload name.
        group: Owning group.
        user: The authenticated caller.
        image: The image to deploy when every region runs the same one (a
            container's). Empty for an offering built per region; ``images``
            carries it then.
        env: Env vars to resolve onto the workload.
        files: File mounts to resolve onto the workload.
        scaling: Autoscaling settings.
        size: Resource t-shirt size.
        hostname: Optional custom host; None takes the default.
        regions: Target region names, or None for all.
        port: The container port to stamp. Always set - both offerings default
            it to 8080 rather than leaving it implicit.
        created: True for a create - enables the absence check and the
            rollback of a half-applied workload, and picks the success status.
        pull_secret_name: Name of the image-pull Secret the KSVC references.
        pull_secret_manifest: The pull Secret to apply, when this offering
            creates one (a container's; a function's is the chart's).
        prev_host: The host the workload currently uses (update only); when it
            differs from the resolved host, the old DomainMapping is retired so
            the old host doesn't stay claimed.
        kept_env: Decoded existing env-Secret values, so a secret env var sent
            without a value keeps its stored value (update only).
        kept_files: Decoded existing files-Secret values, so a secret file sent
            without content keeps its stored content (update only).
        images: The image to run, per region name; takes precedence over
            ``image``. A function builds in each region's own registry, so its
            regions do not run one reference.
        extra_secrets: Owned Secrets applied to every target region (the
            function's git token), so any of them can rebuild after a
            switchover. Not in the managed prune set, so omitting one keeps the
            stored copy.
        region_resources: Owned manifests applied to one region only, keyed by region
            name (a function's ``Image`` and build ServiceAccount). A region
            builds what it runs, so these go to the targets, and each is owned
            by the KSVC beside it.
        runtime: Function runtime, stamped as an annotation.
        version: Requested language version, stamped as an annotation. None
            means the caller took the platform default.
        git_url: Function source repo, stamped as an annotation.
        branch: Function source branch, stamped as an annotation.
        path: Function source sub-directory, stamped as an annotation.
        pull_stamp: The workload's current pull stamp, carried forward so a
            re-composed spec does not drop it and cut a revision.
    """

    name: str
    group: str
    user: Principal
    image: str
    env: list
    files: list
    scaling: object
    size: str
    hostname: str | None
    regions: list[str] | None
    port: int
    created: bool
    pull_secret_name: str | None = None
    pull_secret_manifest: dict | None = None
    prev_host: str | None = None
    kept_env: dict[str, str] | None = None
    kept_files: dict[str, bytes] | None = None
    images: Mapping[str, str] = field(default_factory=dict)
    extra_secrets: Sequence[dict] = field(default_factory=tuple)
    region_resources: Mapping[str, Sequence[dict]] = field(default_factory=dict)
    # Build metadata, stamped as KSVC annotations so a read can report the source
    # a function was built from. All None for an offering that has no build.
    runtime: str | None = None
    version: str | None = None
    git_url: str | None = None
    branch: str | None = None
    path: str | None = None
    pull_stamp: str | None = None

    def image_for(self, region: str) -> str:
        """The image ``region`` should run.

        Args:
            region: The region name.

        Returns:
            That region's own image, falling back to the uniform ``image``.
        """
        return self.images.get(region) or self.image


class WorkloadService:
    """Offering-agnostic orchestration shared by the function/container services."""

    def __init__(
        self,
        settings: Settings,
        deployer: Deployer,
        builder: BuildBackend,
        capacity: StreamCapacity | None = None,
    ):
        """Initialize the engine.

        Args:
            settings: Global settings.
            deployer: The multi-region fan-out helper.
            builder: The function image build backend.
            capacity: The stream pool and admission gate. Defaulted from
                ``settings`` rather than required, so the non-streaming paths -
                which is all of them until a client asks for a stream - can be
                constructed without one.
        """
        self.settings = settings
        self.deployer = deployer
        self.builder = builder
        self._capacity = capacity

    @property
    def capacity(self) -> StreamCapacity:
        """The stream pool; the fallback is built on first streaming use.

        Lazy so an engine constructed without one (tests, scripts) does not
        conjure a thread pool nothing will ever shut down just by existing.
        """
        if self._capacity is None:
            self._capacity = StreamCapacity(self.settings.stream)
        return self._capacity

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

        See :func:`api.services.regions.preflight.resolve_host`.
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

        See :func:`api.services.regions.preflight.validate_spec`.
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

        See :func:`api.services.regions.preflight.assert_deployable`.
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

    def accepted(
        self, offering: Offering, name: str, group: str, host: str, **extra
    ) -> WorkloadResponse:
        """Build the Pending 202 body returned by accept/accept_update.

        Args:
            offering: The offering being deployed.
            name: Workload name.
            group: Owning group.
            host: The resolved external host.
            **extra: Offering-specific fields echoed back (secrets redacted).

        Returns:
            A response with ``status="Pending"`` and a ``statusUrl``.
        """
        return offering.response_model(
            name=name,
            group=group,
            type=offering.name,
            hostname=host,
            status="Pending",
            regions=[],
            statusUrl=f"{api_base()}/groups/{group}/{offering.name}s/{name}",
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
            # The plain-string args only (group, name) - never the whole tuple:
            # the spec in there carries the caller's git/registry tokens and
            # secret values, and this is the one log line that would print them.
            ident = "/".join(a for a in args if isinstance(a, str)) or "?"
            logger.exception("background %s failed for %s", getattr(fn, "__name__", fn), ident)

    async def accept_create(
        self, *, offering: Offering, group: str, spec, user: Principal, background, work, **extra
    ) -> WorkloadResponse:
        """Run a create's synchronous pre-flight, then schedule the deploy (202).

        Shared by both offerings: validate the spec and check the host is free and the
        name unused, so bad input or a conflict is an immediate 400/403/409/503, then
        queue the deploy and return the Pending 202.

        Args:
            offering: The offering being created.
            group: The owning group (from the request path).
            spec: The create request (carries name/regions/hostname/env/files).
            user: The authenticated caller.
            background: FastAPI background tasks to schedule the deploy on.
            work: The offering's background create coroutine, run as
                ``work(group, spec, user)``.
            **extra: Offering-specific fields echoed onto the accepted body.

        Returns:
            A Pending response with a ``statusUrl`` to poll.
        """
        self.assert_group(user, group)
        targets = self.deployer.resolve_targets(spec.regions)
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
        offering: Offering,
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
            offering: The offering being updated.
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
        self, req: ApplyRequest, offering: Offering
    ) -> tuple[WorkloadResponse, int]:
        """Build the manifests once and apply the workload to every target region.

        Applies the KSVC, its derived resources (owned via ownerReferences), and
        the DomainMapping to each region, pruning backing objects the new spec no
        longer references. Offering-agnostic: what differs between a function and
        a container is asked of ``offering``, never branched on here.

        Args:
            req: The apply request (see :class:`ApplyRequest`).
            offering: The offering being deployed.

        Returns:
            The response body and HTTP status code.
        """
        self.assert_group(req.user, req.group)
        oname = object_name(req.name, req.group)
        targets = self.deployer.resolve_targets(req.regions)
        host = self.host_for(req.name, req.hostname, req.group)
        owner = req.user.username

        # Re-checked here, immediately before the mutation, and not merely trusted
        # from accept time: this is the guard, and it has to be the last thing that
        # happens before the apply. The DomainMapping name IS the host, so an
        # idempotent apply would otherwise hijack another workload's mapping; on a
        # create the same pass also confirms the name is still unused, which is why
        # the offering services no longer probe for that separately.
        await self.assert_deployable(
            req.name, req.group, targets, host=host, require_absent=req.created
        )

        resolved = resolve_files(oname, req.group, owner, req.files, req.kept_files)
        resolved_env = resolve_env(oname, req.group, owner, req.env, req.kept_env)
        backing = resolved.backing + resolved_env.backing + list(req.extra_secrets)

        def ksvc_for(region: str) -> dict:
            """Compose the KSVC for one region. Only the image varies."""
            return ksvc_svc.build_ksvc(
                name=oname,
                group=req.group,
                owner=owner,
                image=req.image_for(region),
                offering=offering.name,
                host=host,
                env=resolved_env.env,
                volumes=resolved.volumes,
                scaling=req.scaling,
                size=req.size,
                port=req.port,
                pull_secret=req.pull_secret_name,
                runtime=req.runtime,
                version=req.version,
                git_url=req.git_url,
                branch=req.branch,
                path=req.path,
                ca_config_map=self.settings.ca_bundle.config_map,
                ca_mount_path=self.settings.ca_bundle.mount_path,
                ca_file=self.settings.ca_bundle.file,
                pull_stamp=req.pull_stamp,
            )

        mapping = route_svc.build_domain_mapping(
            name=oname, group=req.group, owner=owner, offering=offering.name, host=host
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
        } | offering.managed_secrets(oname)
        # Keyed on whether the KSVC still references it, not on the manifest: the
        # secret can be carried forward without being re-applied.
        if req.pull_secret_name:
            applied_derived.add((ResourceKind.SECRET, req.pull_secret_name))
        to_prune = () if req.created else managed_derived - applied_derived

        # Before any apply: a moved tag cannot be applied over, only replaced.
        await self.retag_build(targets, req.region_resources)

        def apply(cluster: Cluster) -> RegionStatus:
            # A region builds what it runs, so its build objects ride along with
            # its KSVC and are owned by it - there is no unowned case left.
            return region_apply.apply_to_region(
                cluster,
                oname=oname,
                ksvc=ksvc_for(cluster.region),
                backing=backing + list(req.region_resources.get(cluster.region, ())),
                pull_secret_manifest=req.pull_secret_manifest,
                mapping=mapping,
                to_prune=to_prune,
                created=req.created,
                prev_host=req.prev_host,
            )

        statuses = await self.deployer.fanout(targets, apply)
        overall = aggregate(statuses)
        common = dict(
            name=req.name,
            group=req.group,
            type=offering.name,
            hostname=host,
            status=overall,
            size=req.size,
            regions=statuses,
            scaling=req.scaling,
            env=describe_svc.redact_env(req.env),
            files=describe_svc.redact_files(req.files),
            createdAt=datetime.now(ISRAEL_TZ) if req.created else None,
        )
        return offering.applied_response(common, req), status_code_for(overall, created=req.created)

    def target_registries(self, requested: list[str] | None) -> dict[str, RegistryConfig]:
        """The registry each targeted region builds into, keyed by region name.

        What a build plan needs, taken off the clusters rather than re-resolved
        from settings, so there is one answer per region and not two snapshots
        of it.

        Args:
            requested: Explicit region names, or None for all configured regions.

        Returns:
            ``{region: registry}`` for the targets.
        """
        return {c.region: c.registry for c in self.deployer.resolve_targets(requested)}

    async def apply_build(self, name: str, group: str, plan: BuildPlan) -> bool:
        """Re-declare a workload's build in every region that runs it, then ask for one.

        The rebuild path, and the one write in the engine that leaves the KSVC
        alone: nothing about the workload's desired state changes, so there is
        no spec to fan out - only the build objects and the trigger.

        Applying before triggering is what makes this self-healing rather than
        merely idempotent: a region that has never built the function gets the
        Image created here and builds from that, and
        :meth:`~common.build.BuildBackend.trigger` finds nothing to annotate and
        says so.

        Args:
            name: The workload name.
            group: The owning group.
            plan: The build plan to apply (its git Secret included, so the region
                that builds can always clone).

        Returns:
            True if an existing build was triggered in any region; False if
            applying the plan is itself what starts them.
        """
        oname = object_name(name, group)
        targets = self.deployer.resolve_targets(None)
        await self.retag_build(targets, plan.manifests_by_region)

        def work(cluster: Cluster) -> RegionStatus:
            manifests = list(plan.replicated) + plan.manifests_for(cluster.region)
            # Skips a region the workload does not run in, which is also every
            # region the plan does not cover.
            if not region_apply.apply_build_objects(cluster, manifests, oname=oname):
                return RegionStatus(region=cluster.region, status="Absent")
            triggered = self.builder.trigger(cluster, name, group)
            return RegionStatus(
                region=cluster.region, status="Building" if triggered else "Pending"
            )

        statuses = await self.deployer.fanout(targets, work)
        return any(s.status == "Building" for s in statuses)

    async def retag_build(
        self, targets: list[Cluster], region_resources: Mapping[str, Sequence[dict]]
    ) -> None:
        """Make way for an Image whose tag has moved, and reclaim what it left.

        ``spec.tag`` is **immutable** on a kpack Image, so applying a moved one is
        rejected at admission - which would wedge every later write to the
        function, not just the layout change. The Image is deleted instead and
        the apply that follows recreates it; a new Image with no prior Build
        builds on its own, so nothing else has to ask for one.

        Per region, because each region's tag moves independently - which is what
        makes moving an install onto per-region registries a re-apply rather than
        a migration (docs/BUILDING.md - Moving a function's repository).

        A no-op in the normal case, which is what the tag comparison buys - the
        cost is one GET per region per write.

        Args:
            targets: The clusters being written to.
            region_resources: Each region's build manifests; its Image is picked out.
        """
        if not region_resources:
            return
        await asyncio.gather(
            *(self._retag_region(c, region_resources.get(c.region, ())) for c in targets)
        )

    async def _retag_region(self, cluster: Cluster, manifests: Sequence[dict]) -> None:
        """Re-tag one region's Image, reclaiming the repository it leaves behind.

        Args:
            cluster: The region to re-tag in.
            manifests: That region's build manifests.
        """
        desired = next((m for m in manifests if m.get("kind") == "Image"), None)
        if desired is None:
            return
        name = (desired.get("metadata") or {}).get("name")
        want = (desired.get("spec") or {}).get("tag")

        def retag() -> str | None:
            try:
                current = cluster.get(ResourceKind.KPACK_IMAGE, name)
            except NotFoundError:
                return None  # nothing built here yet; the apply creates it
            had = ((current.get("spec") or {}).get("tag")) or None
            if not had or had == want:
                return None
            cluster.delete(ResourceKind.KPACK_IMAGE, name)
            logger.info(
                "Image '%s' re-tagged from '%s' to '%s' in %s", name, had, want, cluster.region
            )
            return had

        try:
            previous = await asyncio.to_thread(retag)
        except Exception:  # noqa: BLE001 - the apply below reports the real failure
            logger.exception("could not re-tag Image '%s' in %s", name, cluster.region)
            return
        if previous:
            # This region's registry: a reference on another host is somebody
            # else's repository, and reclaim already refuses to touch it.
            await asyncio.to_thread(
                registry_svc.reclaim_moved_repositories, cluster.registry, previous
            )

    async def stamp_pull(self, name: str, group: str, stamp: str) -> list[RegionStatus]:
        """Stamp a new pull marker on the workload in every region.

        Changing a ``spec.template`` annotation is what makes Knative cut a
        revision and resolve the tag again. A merge patch rather than the usual
        full apply, like the rebuild trigger: no desired state changes
        (docs/CONTAINERS.md - Pulling the tag again).

        Args:
            name: The workload name.
            group: The owning group.
            stamp: The value to write, the same in every region so they cannot
                drift onto different revisions.

        Returns:
            One status per region; ``Absent`` where the workload does not run.
        """
        oname = object_name(name, group)
        patch = {
            "metadata": {"annotations": {ANNOTATION_PULL_STAMP: stamp}},
            "spec": {"template": {"metadata": {"annotations": {ANNOTATION_PULL_STAMP: stamp}}}},
        }

        def stamp_region(cluster: Cluster) -> RegionStatus:
            try:
                cluster.patch(ResourceKind.KNATIVE_SERVICE, oname, patch)
            except NotFoundError:
                return RegionStatus(region=cluster.region, status="Absent")
            return RegionStatus(region=cluster.region, status="Deploying")

        return await self.deployer.fanout(self.deployer.resolve_targets(None), stamp_region)

    async def load_existing(
        self, name: str, offering: Offering, user: Principal, group: str
    ) -> dict:
        """Fetch an existing workload's carried-forward state (offering-scoped).

        Reads from whichever region has the workload; a down region is never reported
        as a missing workload.

        Args:
            name: The workload name.
            offering: The expected offering; another one's workload of the same
                name is hidden as a 404 rather than loaded.
            user: The authenticated caller.
            group: The owning group.

        Returns:
            A dict with the image, the image per region, and carried-forward
            build/pull metadata.

        Raises:
            NotFoundError: If it doesn't exist or isn't this offering/group.
            ServiceUnavailableError: If it couldn't be confirmed absent because a
                region was unreachable.
        """
        self.assert_group(user, group)
        oname = object_name(name, group)
        targets = self.deployer.resolve_targets(None)
        found: dict = {}
        images: dict[str, str] = {}

        def fetch(cluster: Cluster) -> RegionStatus:
            # Only a real 404 means absent; anything else must propagate so a down region
            # is recorded as an error, not mistaken for absence.
            try:
                obj = cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
            except NotFoundError:
                return RegionStatus(region=cluster.region, status="Absent")
            # The spec is uniform across regions, so any responder's copy will do;
            # setdefault is atomic under the concurrent fan-out. The image is the
            # exception - each region runs what its own build pushed - so it is
            # kept per region, and an update carries each one forward untouched.
            found.setdefault("obj", obj)
            deployed = ksvc_state.extract_image(obj)
            if deployed:
                images[cluster.region] = deployed
            return RegionStatus(region=cluster.region, status="Present")

        statuses = await self.deployer.fanout(targets, fetch)

        obj = found.get("obj")
        if obj is not None:
            # An object_name collision could resolve to another group's workload or
            # the other offering; both mean "not this workload" -> hide as 404.
            if not ownership.owned_by(obj, user, offering.name):
                raise NotFoundError(f"{offering.name} workload '{name}' not found")
            # Read the backing Secrets from the local region when it has the workload,
            # else any region that does - they're uniform, so prefer the cheapest hop.
            present = {s.region for s in statuses if s.status == "Present"}
            if not present:
                # `obj` was found but no region reported Present: a fan-out that
                # timed out can leave a zombie thread's late write in `found`
                # with its status recorded as an error. Fail like the
                # unreachable-region case below rather than crash on an empty set.
                preflight.assert_all_regions_checked(statuses, f"load workload '{name}'")
                raise NotFoundError(f"{offering.name} workload '{name}' not found")
            by_region = {c.region: c for c in targets}
            local = self.deployer.local_region()
            cluster = by_region[local if local in present else next(iter(present))]
            # Reading the backing Secrets is blocking cluster I/O; run it in a thread
            # so it doesn't stall the event loop (as get()/describe_spec do).
            state = await asyncio.to_thread(
                region_read.existing_state, obj, cluster, offering, oname
            )
            return {**state, "images": images}

        # Absent on every region we could reach. If one was unreachable we can't be
        # sure it's truly gone -> fail closed (503), not a misleading 404.
        preflight.assert_all_regions_checked(statuses, f"load workload '{name}'")
        raise NotFoundError(f"{offering.name} workload '{name}' not found")

    async def get(
        self, offering: Offering, name: str, user: Principal, group: str
    ) -> WorkloadResponse:
        """Read one workload with live per-region status and its redacted spec.

        Fans out to all regions; a region that returns a clean 404 is omitted, while
        an unreachable region stays visible and degrades the rollup.

        Args:
            offering: The offering being read.
            name: Workload name.
            user: The authenticated caller.
            group: Owning group.

        Returns:
            The full single-workload response.

        Raises:
            NotFoundError: If the workload exists on no reachable region.
            ServiceUnavailableError: If it can't be confirmed absent because a
                region was unreachable.
        """
        self.assert_group(user, group)
        kind = offering.name  # the API kind ("function"/"container") is the offering label
        oname = object_name(name, group)
        meta_holder: dict[str, str] = {}
        # The spec is uniform across regions, so read it back once from one
        # representative region (local if it has the workload) after the fan-out.
        reps: dict[str, tuple] = {}
        # The build is NOT uniform: each region builds its own copy, so its state
        # is read in the same per-region thread as that region's KSVC.
        builds: dict[str, BuildStatusView | None] = {}

        def fetch(cluster: Cluster) -> RegionStatus | None:
            # A 404 means not deployed here, so omit the region rather than fail it.
            # Anything else propagates, keeping a down region visible as Failed.
            try:
                obj = cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
            except NotFoundError:
                return None
            annotations = (obj.get("metadata", {}) or {}).get("annotations", {}) or {}
            for key, ann in (("host", ANNOTATION_HOST), ("size", ANNOTATION_SIZE)):
                if ann in annotations and key not in meta_holder:
                    meta_holder[key] = annotations[ann]
            reps[cluster.region] = (obj, cluster)
            if offering.has_build:
                builds[cluster.region] = offering.build_status(self.builder, cluster, name, group)
            status, revision = ksvc_state.ksvc_status(obj)
            # One read, and it earns its place twice over: the Revision carries the
            # live scale and the specific rollout-failure condition. The usage read
            # that used to sit beside it is gone from this path - it is a cluster
            # call for something only a poller wants, and pollers have /status.
            rev = region_read.revision(cluster, revision)
            replicas = ksvc_state.revision_replicas(rev)
            # Prefer the Revision's conditions (the specific cause) over the KSVC's, so
            # a GET explains why it failed instead of a bare status=Failed.
            message = None
            reason = None
            if status == "Failed":
                message = revision_failure_message(rev) or ksvc_failure_message(obj)
                reason = ksvc_state.failure_cause(rev, obj)
            return RegionStatus(
                region=cluster.region,
                status=status,
                revision=revision,
                reason=reason,
                message=message,
                replicas=replicas,
            )

        targets = self.deployer.resolve_targets(None)
        results = await self.deployer.fanout(targets, fetch)
        statuses = [s for s in results if s is not None]  # drop regions without it

        if not reps:
            # Present on no reachable region. If a region was unreachable we can't be
            # sure it's absent -> 503; otherwise it's genuinely gone -> 404.
            preflight.assert_all_regions_checked(statuses, f"get workload '{name}'")
            raise NotFoundError(f"{kind} '{name}' not found")

        # The spec is uniform across regions: read it (and authorize) from the local
        # region if it has the workload, else any region that does.
        obj, cluster = reps.get(self.deployer.local_region()) or next(iter(reps.values()))
        if not ownership.owned_by(obj, user, kind):
            raise _hidden_404("get", kind, name, user, obj)

        host = meta_holder.get("host", route_svc.host_for(name, group, self.settings.route_domain))
        # A down region counts as Failed; otherwise the per-region KSVC
        # status drives the rollup, so a workload still coming up reads as Deploying.
        overall = overall_status_for_regions(statuses)
        # Build-first, per region and then rolled up: while a region is building, its
        # KSVC cannot pull an image that does not exist there yet, so the row
        # would contradict the headline it sits under (docs/FUNCTIONS.md -
        # Function Status Resolution).
        # Snapshot first: a fan-out that timed a region out leaves a zombie thread
        # that may still insert into `builds` while these iterate. dict/list
        # copies are atomic under the GIL; iterating the live view is not.
        builds = dict(builds)
        build = ksvc_state.roll_up_builds(list(builds.values()))
        statuses = ksvc_state.regions_with_build_status(statuses, builds)
        overall = ksvc_state.with_build_status(overall, build)
        # The headline reason, Kubernetes-style: the build verdict is
        # authoritative (a failed build IS the cause, wherever the rows stand -
        # regions still serving their previous revision stay Ready), else the
        # first recognized per-region cause.
        if build is not None and build.state == "Failed":
            status_reason = "BuildFailed"
        else:
            status_reason = next((s.reason for s in statuses if s.reason), None)
        spec = await asyncio.to_thread(region_read.describe_spec, cluster, obj)
        # Neither `obj` nor `spec` is optional from here: `reps` is non-empty
        # (guarded above) and every entry holds an object, and describe_spec
        # always returns a WorkloadSpec. Guarding them would advertise a nullable
        # that does not exist, which is how a real one stops being noticeable.
        common = dict(
            name=name,
            group=group,
            type=kind,
            hostname=host,
            status=overall,
            reason=status_reason,
            size=meta_holder.get("size"),
            createdAt=ksvc_state.creation_time(obj),
            regions=statuses,
            scaling=spec.scaling,
            env=spec.env,
            files=spec.files,
        )
        return offering.fetched_response(common, obj, spec, build)

    async def stats(
        self,
        offering: Offering,
        name: str,
        user: Principal,
        group: str,
        *,
        executor: Executor | None = None,
    ) -> WorkloadStatsResponse:
        """Read a workload's live state: rollup, and per-region replicas and usage.

        The poll counterpart to :meth:`get`. Same fan-out, authorization and
        rollup, but none of the desired-state reads - no file ConfigMaps and no
        backing Secret - so a client refreshing every few seconds is not pulling
        secret material out of the cluster on a loop for config that only changes
        when it changes it.

        The build is still read for a function, though it is not reported here:
        it is what makes a running build ``Building`` instead of the ``Failed``
        its unpullable image would otherwise produce (docs/FUNCTIONS.md -
        Function Status Resolution).

        Args:
            offering: The offering being read.
            name: Workload name.
            user: The authenticated caller.
            group: Owning group.
            executor: Pool the per-region reads run on; None takes the default.
                The stats stream passes its own, because a reading it repeats for
                as long as a client is connected must not compete for the threads
                every ordinary request needs.

        Returns:
            The live stats view.

        Raises:
            NotFoundError: If the workload exists on no reachable region.
            ServiceUnavailableError: If it can't be confirmed absent because a
                region was unreachable.
        """
        self.assert_group(user, group)
        kind = offering.name  # the API kind ("function"/"container") is the offering label
        oname = object_name(name, group)
        reps: dict[str, dict] = {}
        # Raw, because the workload total is summed from these rather than from
        # the rounded figures the response carries.
        usage_by_region: dict[str, region_read.RegionUsage] = {}
        builds: dict[str, BuildStatusView | None] = {}

        def fetch(cluster: Cluster) -> RegionStatus | None:
            try:
                obj = cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
            except NotFoundError:
                return None  # not deployed here; omit the region rather than fail it
            reps[cluster.region] = obj
            if offering.has_build:
                builds[cluster.region] = offering.build_status(self.builder, cluster, name, group)
            status, revision = ksvc_state.ksvc_status(obj)
            usage_by_region[cluster.region] = region_read.region_usage(cluster, oname)
            rev = region_read.revision(cluster, revision)
            return RegionStatus(
                region=cluster.region,
                status=status,
                replicas=ksvc_state.revision_replicas(rev),
                message=revision_failure_message(rev) if status == "Failed" else None,
                reason=ksvc_state.failure_cause(rev, obj) if status == "Failed" else None,
            )

        targets = self.deployer.resolve_targets(None)
        results = await self.deployer.fanout(targets, fetch, executor=executor)
        statuses = [s for s in results if s is not None]  # drop regions without it

        if not reps:
            # If a region was unreachable we can't be sure it's absent -> 503.
            preflight.assert_all_regions_checked(statuses, f"get stats of workload '{name}'")
            raise NotFoundError(f"{kind} '{name}' not found")

        obj = reps.get(self.deployer.local_region()) or next(iter(reps.values()))
        if not ownership.owned_by(obj, user, kind):
            raise _hidden_404("stats of", kind, name, user, obj)

        overall = overall_status_for_regions(statuses)
        # Not reported here, but it is what makes a running build read as
        # `Building` instead of the `Failed` its unpullable image would
        # otherwise produce (docs/FUNCTIONS.md - Function Status Resolution).
        # Read per region inside `fetch`, so it rides the same fan-out - and so a
        # stats *stream* pays for it on its own pool, like every other read here.
        # Snapshot first - same zombie-thread hazard as get()'s rollup above.
        builds = dict(builds)
        rolled = ksvc_state.roll_up_builds(list(builds.values()))
        overall = ksvc_state.with_build_status(overall, rolled)
        statuses = ksvc_state.regions_with_build_status(statuses, builds)

        # Both totals are null rather than partial when a region could not answer:
        # a single number quietly missing a whole region still reads as authoritative.
        reads = [usage_by_region.get(s.region) for s in statuses]
        usage = None
        if all(r is not None and r.measured for r in reads):
            usage = metrics_svc.total(r.total for r in reads if r.total)
        replicas = None
        if all(s.replicas is not None for s in statuses):
            replicas = sum(s.replicas for s in statuses)

        # As on the full GET: the build verdict is authoritative, else the
        # first recognized per-region cause.
        if rolled is not None and rolled.state == "Failed":
            reason = "BuildFailed"
        else:
            reason = next((s.reason for s in statuses if s.reason), None)

        return WorkloadStatsResponse(
            status=overall,  # type: ignore[arg-type]
            reason=reason,
            replicas=replicas,
            usage=usage.quantities() if usage else None,
            regions=[
                RegionStats(
                    region=s.region,
                    status=s.status,
                    reason=s.reason,
                    replicas=s.replicas,
                    usage=u.total.quantities() if u and u.total else None,
                )
                for s, u in zip(statuses, reads, strict=True)
            ],
        )

    async def delete(self, offering: Offering, name: str, user: Principal, group: str) -> None:
        """Delete a workload from every region; GC cascades its derived resources.

        Args:
            offering: The offering being deleted.
            name: Workload name.
            user: The authenticated caller.
            group: Owning group.

        Raises:
            NotFoundError: If the workload exists on no region, or the caller may
                not access it (hidden as 404, matching GET).
            ServiceUnavailableError: If any region could not be reached, so the
                delete cannot be confirmed.
        """
        self.assert_group(user, group)
        kind = offering.name  # the API kind ("function"/"container") is the offering label
        oname = object_name(name, group)
        denied: list[str] = []

        def remove(cluster: Cluster) -> RegionStatus:
            # A clean 404 means "not deployed here", which is not a failure and must
            # not read as one - only a region that cannot answer at all is an error.
            try:
                obj = cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
            except NotFoundError:
                return RegionStatus(region=cluster.region, status="Absent")
            # Recorded, not raised: raising here would be caught by the fan-out and
            # become indistinguishable from an unreachable region.
            if not ownership.owned_by(obj, user, kind):
                denied.append(cluster.region)
                return RegionStatus(region=cluster.region, status="Denied")
            # Cascades to every owned resource: the config Secrets/ConfigMap, the
            # pull secret, the DomainMapping, the build objects.
            try:
                cluster.delete(ResourceKind.KNATIVE_SERVICE, oname)
            except NotFoundError:
                return RegionStatus(region=cluster.region, status="Absent")  # raced a peer
            return RegionStatus(region=cluster.region, status="Deleted")

        targets = self.deployer.resolve_targets(None)
        statuses = await self.deployer.fanout(targets, remove)

        # An unreachable region cannot confirm the workload is gone. Fail closed (503)
        # so the caller retries, rather than reporting a 404 that reads as "already
        # deleted" while the workload is still serving somewhere (delete is
        # idempotent, so a retry over the regions that did succeed is a no-op).
        preflight.assert_all_regions_checked(statuses, f"delete {kind} '{name}'")
        if denied:
            logger.debug(
                "delete %s '%s' denied for user %s at %s; hidden as 404",
                kind,
                name,
                user.username,
                ", ".join(sorted(denied)),
            )
            raise NotFoundError(f"{kind} '{name}' not found")

        # Every region answered and none refused, so the workload is gone platform-wide
        # and whatever the ownerReferences did not cascade to can go too. Reached
        # even when nothing was deleted: that is the case where an earlier partial
        # delete orphaned them, and a leftover Image would keep rebuilding a
        # function nothing runs.
        # Per region, because what is left behind is per region: each built its own
        # image, into its own registry, from its own build objects.
        await asyncio.gather(
            *(
                asyncio.to_thread(
                    offering.after_delete,
                    DeleteContext(cluster=cluster, oname=oname, name=name, group=group),
                )
                for cluster in targets
            )
        )
        if all(s.status == "Absent" for s in statuses):
            raise NotFoundError(f"{kind} '{name}' not found")

    async def stream_pods(
        self,
        offering: Offering,
        name: str,
        user: Principal,
        group: str,
        *,
        interval: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream which pods the workload has on the local region.

        The endpoint that makes per-pod log streaming usable: a pod name is what
        ``stream_pod_logs`` takes, and nothing else in the API returns one.

        Local region only, matching the log streams it feeds - a pod name is only
        useful where its log can be read.

        The first roster is read here rather than in the stream, because it is
        also what authorizes the request: a workload that does not exist is a 404
        with an envelope, not a stream that opens and immediately errors.

        Args:
            offering: The offering being read.
            name: Workload name.
            user: The authenticated caller.
            group: Owning group.
            interval: Seconds between listings; None takes the configured default.

        Returns:
            The event stream, beginning with a ``pods`` event.

        Raises:
            NotFoundError: If the workload isn't on the local region or the caller
                can't access it (hidden as 404, matching GET).
            ServiceUnavailableError: If no stream slot is free.
        """
        self.assert_group(user, group)
        kind = offering.name  # the API kind ("function"/"container") is the offering label
        oname = object_name(name, group)
        cluster = self.deployer.local_cluster()

        def read() -> list:
            obj = cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
            if not ownership.owned_by(obj, user, kind):
                raise _hidden_404("stream pods of", kind, name, user, obj)
            return pods_stream.read_roster(cluster, oname)

        slot = self.capacity.admit()
        try:
            roster = await self.capacity.run(read)
        except BaseException:
            slot.release()
            raise

        first = PodRoster(
            name=name,
            group=group,
            type=kind,  # type: ignore[arg-type]
            region=self.deployer.local_region(),
            pods=roster,
        )

        return _slot_guarded(
            slot,
            pods_stream.follow(
                cluster=cluster,
                capacity=self.capacity,
                config=self.capacity.config,
                first=first,
                oname=oname,
                interval=self.capacity.interval(interval),
            ),
        )

    def _pod_authorizer(
        self, cluster: Cluster, oname: str, pod: str, kind: str, name: str, user: Principal
    ):
        """The check both log reads run, as one blocking callable.

        Shared rather than written twice because the second half is the security
        boundary: owning the workload is not owning every pod, and the pods of
        every workload on the platform sit in one namespace. Two copies of that
        rule is one copy that gets fixed.

        Args:
            cluster: The local region.
            oname: The object name (``{name}-{group}``).
            pod: The pod the caller named.
            kind: The offering label ("function"/"container").
            name: The workload name, for the error message.
            user: The authenticated caller.

        Returns:
            A callable returning the pod's revision, to run off the event loop.
        """

        def authorize() -> str | None:
            obj = cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
            if not ownership.owned_by(obj, user, kind):
                raise _hidden_404("read logs of", kind, name, user, obj)
            found = cluster.get(ResourceKind.POD, pod)
            labels = (found.get("metadata", {}) or {}).get("labels", {}) or {}
            if labels.get(pods_stream.SERVICE_LABEL) != oname:
                # Someone else's pod, or none of ours. Same answer as absent: the
                # response must not confirm that a pod by this name exists.
                logger.debug("pod '%s' is not a pod of %s '%s'; hidden as 404", pod, kind, name)
                raise NotFoundError(f"pod '{pod}' not found")
            return labels.get(pods_stream.REVISION_LABEL)

        return authorize

    async def pods(self, offering: Offering, name: str, user: Principal, group: str) -> PodRoster:
        """The workload's pods on the local region, read once (``follow=false``).

        The non-streaming half of :meth:`stream_pods`, and the same reads. It
        exists for the caller that cannot hold a connection - without it, a
        server-side client could not learn a pod name, and so could not use the
        log snapshot either.

        No stream slot: this is an ordinary bounded request that ends, and
        charging it against the streaming pool would let held-open connections
        throttle a caller that is not holding one.

        Args:
            offering: The offering being read.
            name: Workload name.
            user: The authenticated caller.
            group: Owning group.

        Returns:
            The roster.

        Raises:
            NotFoundError: If the workload isn't on the local region or the caller
                can't access it (hidden as 404, matching GET).
        """
        self.assert_group(user, group)
        kind = offering.name  # the API kind ("function"/"container") is the offering label
        oname = object_name(name, group)
        cluster = self.deployer.local_cluster()

        def read() -> list:
            obj = cluster.get(ResourceKind.KNATIVE_SERVICE, oname)
            if not ownership.owned_by(obj, user, kind):
                raise _hidden_404("read pods of", kind, name, user, obj)
            return pods_stream.read_roster(cluster, oname)

        return PodRoster(
            name=name,
            group=group,
            type=kind,  # type: ignore[arg-type]
            region=self.deployer.local_region(),
            pods=await asyncio.to_thread(read),
        )

    async def pod_logs(
        self,
        offering: Offering,
        name: str,
        user: Principal,
        group: str,
        *,
        pod: str,
        container: str,
        since_seconds: int | None,
        limit_bytes: int | None,
        tail_lines: int | None = None,
    ) -> PodLogSnapshot:
        """One pod's log as it stands, read once (``follow=false``).

        The non-streaming half of :meth:`stream_pod_logs`, through the same
        authorization, so the pod-ownership rule cannot differ between them. What
        it returns is bounded by what the node still holds - there is no history
        behind that, whichever way it is read - and further by the configured
        snapshot bounds: the newest ``snapshot_tail_lines`` lines, within
        ``snapshot_max_bytes``. Bounded *here*, not left to the caller, because
        the node can hold tens of megabytes and this read shares a process with
        the health probes: an unbounded snapshot parsed into one response is
        how a polling client makes the API unready.

        No stream slot: the read ends, so it is not a stream and must not be
        rationed as one.

        Args:
            offering: The offering being read.
            name: Workload name.
            user: The authenticated caller.
            group: Owning group.
            pod: The pod to read.
            container: The pod container to read.
            since_seconds: Only lines newer than this, if set.
            limit_bytes: Cap on the bytes read, if set; clamped to the
                configured ceiling either way.
            tail_lines: Newest lines wanted, if set; clamped to the configured
                snapshot bound either way.

        Returns:
            The snapshot.

        Raises:
            NotFoundError: If the workload or the pod isn't here, the pod is not
                this workload's, or the caller can't access it (all hidden as 404).
        """
        self.assert_group(user, group)
        kind = offering.name  # the API kind ("function"/"container") is the offering label
        oname = object_name(name, group)
        cluster = self.deployer.local_cluster()
        authorize = self._pod_authorizer(cluster, oname, pod, kind, name, user)

        config = self.capacity.config
        # tail_lines picks the *newest* lines; limit_bytes truncates from the
        # start of what was picked. Applied together, the tail does the real
        # bounding and the byte ceiling backstops pathological line lengths.
        capped_bytes = min(limit_bytes or config.snapshot_max_bytes, config.snapshot_max_bytes)
        capped_tail = min(tail_lines or config.snapshot_tail_lines, config.snapshot_tail_lines)

        def read() -> tuple[str | None, list[LogLine]]:
            revision = authorize()
            text = cluster.pod_logs(
                pod,
                container=container,
                since_seconds=since_seconds,
                limit_bytes=capped_bytes,
                tail_lines=capped_tail,
            )
            # Split exactly as the stream splits, so a client renders one shape
            # whichever way it read the log. On this thread, not the event
            # loop: even bounded, it is regex-and-model work per line.
            return revision, [
                LogLine(
                    pod=pod,
                    container=container,
                    revision=revision,
                    time=stamp,
                    message=message,
                )
                for stamp, message in (
                    logs_stream.split_timestamp(line) for line in text.splitlines()
                )
            ]

        revision, lines = await asyncio.to_thread(read)
        return PodLogSnapshot(
            name=name,
            group=group,
            type=kind,  # type: ignore[arg-type]
            region=self.deployer.local_region(),
            pod=pod,
            container=container,
            revision=revision,
            lines=lines,
        )

    async def stream_pod_logs(
        self,
        offering: Offering,
        name: str,
        user: Principal,
        group: str,
        *,
        pod: str,
        container: str,
        since_seconds: int | None,
        tail_lines: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Follow one of the workload's pods' logs, on the local region.

        Local region only: Kubernetes keeps no log buffer beyond the node that
        wrote it, so there is nowhere else to read from.

        The pod is authorized twice over, and the second check is the one that
        matters. Owning the workload is not enough - the caller names a pod, and
        the pods of every workload on the platform share one namespace - so the
        named pod must also carry this workload's service label. Without that,
        any authenticated user could read any pod in the namespace by guessing
        its name.

        Args:
            offering: The offering being read.
            name: Workload name.
            user: The authenticated caller.
            group: Owning group.
            pod: The pod to follow, as ``stream_pods`` named it.
            container: The pod container to read.
            since_seconds: How far back the log starts, so a client sees recent
                context rather than only what arrives after it connected.
            tail_lines: Start at the newest this-many lines instead, however old
                they are - what a viewer opening an *existing* pod wants, since
                a pod quiet for longer than any time window would otherwise
                show nothing until it next writes. Clamped to the snapshot
                bound: it is the same "history a client gets at once".

        Returns:
            The event stream, beginning with an ``open`` event.

        Raises:
            NotFoundError: If the workload or the pod isn't here, the pod is not
                this workload's, or the caller can't access it (all hidden as 404).
            ServiceUnavailableError: If no stream slot is free.
        """
        self.assert_group(user, group)
        kind = offering.name  # the API kind ("function"/"container") is the offering label
        oname = object_name(name, group)
        cluster = self.deployer.local_cluster()
        authorize = self._pod_authorizer(cluster, oname, pod, kind, name, user)

        slot = self.capacity.admit()
        try:
            revision = await self.capacity.run(authorize)
        except BaseException:
            slot.release()
            raise

        opening = PodLogStreamOpen(
            name=name,
            group=group,
            type=kind,  # type: ignore[arg-type]
            region=self.deployer.local_region(),
            pod=pod,
            container=container,
            revision=revision,
        )

        config = self.capacity.config
        return _slot_guarded(
            slot,
            logs_stream.follow(
                cluster=cluster,
                capacity=self.capacity,
                config=config,
                opening=opening,
                since_seconds=since_seconds,
                tail_lines=(
                    min(tail_lines, config.snapshot_tail_lines) if tail_lines is not None else None
                ),
            ),
        )

    async def stream_stats(
        self,
        offering: Offering,
        name: str,
        user: Principal,
        group: str,
        *,
        interval: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Push the workload's live state on an interval, as a stream of events.

        The streaming counterpart to :meth:`stats`, and identical in what it
        reports - the same rollup, the same per-region replicas and usage, a
        function's build read for the same reason. Only the transport differs.

        The first reading is taken here rather than in the stream: it is what
        authorizes the request, so a workload that does not exist is a 404 with
        an envelope instead of a stream that opens and immediately errors.

        Args:
            offering: The offering being read.
            name: Workload name.
            user: The authenticated caller.
            group: Owning group.
            interval: Seconds between readings; None takes the configured default.

        Returns:
            The event stream, beginning with a ``stats`` event.

        Raises:
            NotFoundError: If the workload exists on no reachable region.
            ServiceUnavailableError: If no stream slot is free, or the workload
                can't be confirmed absent because a region was unreachable.
        """
        config = self.capacity.config

        async def read() -> WorkloadStatsResponse:
            return await self.stats(offering, name, user, group, executor=self.capacity.executor)

        slot = self.capacity.admit()
        try:
            first = await read()
        except BaseException:
            slot.release()
            raise

        return _slot_guarded(
            slot,
            stats_stream.follow(
                config=config,
                first=first,
                read=read,
                interval=self.capacity.interval(interval),
            ),
        )

    async def list(
        self, offering: Offering, user: Principal, group: str, sort: str = "name"
    ) -> list[WorkloadSummary]:
        """Summarize every workload of this offering owned by ``group``.

        Fans out to all regions and merges best-effort: a workload's ``regions`` lists only
        those that returned it, and its rollup covers just those, so a single-region
        workload reads ``Ready``, not ``Failed``. An unreachable region is skipped; only
        an all-down fan-out fails the call.

        Build-first, like the single GET (docs/FUNCTIONS.md - Function Status
        Resolution): a function whose first build is still running has a KSVC that
        cannot pull its image yet, so a listing that read the KSVC alone showed
        every new function as ``Failed`` for the whole of that build. The build
        states come from one label-selected read of the local region, so the fold
        costs a single round trip for the entire listing - overlapped with the
        fan-out, not chained onto it.

        Args:
            offering: The offering being listed.
            user: The authenticated caller.
            group: The owning group.
            sort: Sort key, "name" or "createdAt" (default "name").

        Returns:
            The per-workload summaries.

        Raises:
            RegionTotalFailure: If every region is unreachable.
        """
        self.assert_group(user, group)
        kind = offering.name  # the API kind ("function"/"container") is the offering label
        selector = f"{LABEL_GROUP}={group},{LABEL_OFFERING}={kind}"

        def fetch(cluster: Cluster) -> tuple[list[dict], dict]:
            # Both reads in one per-region thread: the build states belong to this
            # region now, so pairing them costs no extra round trip and cannot
            # attribute one region's builds to another. Branching on the declared
            # capability, not on which offering this is - an offering with no
            # build must not pay for a read that returns {}.
            ksvcs = cluster.get(ResourceKind.KNATIVE_SERVICE, label_selector=selector)
            if not offering.has_build:
                return ksvcs, {}
            return ksvcs, offering.build_states(self.builder, cluster, group)

        results = await self.deployer.gather_each(self.deployer.resolve_targets(None), fetch)
        if all(read is None for _, read in results):
            # Same {region, message} shape as aggregate's total-failure; gather_each
            # keeps no per-region error, so message is None.
            raise RegionTotalFailure(
                "Listing failed in all regions.",
                details=[{"region": region, "message": None} for region, _ in results],
            )

        return summaries_svc.merge(
            [(region, read[0] if read else None) for region, read in results],
            group=group,
            offering=kind,
            builds={region: read[1] for region, read in results if read},
            route_domain=self.settings.route_domain,
            sort=sort,
        )
