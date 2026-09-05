"""Shared workload engine: build manifests once, fan out to all regions.

Offering-agnostic. FunctionService and ContainerService compose this engine and
add only the offering-specific prep (build-from-Git vs image + pull secret);
apply, host/absence checks, access control and get/delete all live here.

What lives here is the *orchestration* - which regions to visit, in what order,
and what a partial answer means (docs/API.md - Partial-failure
semantics). What it orchestrates lives in modules of its own:

* :mod:`api.services.manifests` - build what gets applied (pure)
* :mod:`api.services.regions`     - fan out, and write/read one region
* :mod:`api.services.state`     - interpret what came back (pure)
* :mod:`api.services.builder`   - the function image build

The ``assert_*``/``host_for``/``validate_spec`` methods below are thin
delegations to :mod:`api.services.regions.preflight`; the offering services and
the routers hold this engine and call them on it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass
from datetime import datetime

from cloudlet_apis.auth import Principal
from cloudlet_apis.logging import get_logger

from api.core.config import Settings
from api.core.paths import api_base
from api.models.common import (
    ANNOTATION_GIT_COMMIT,
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
from api.services.regions.deployer import Deployer
from api.services.regions.rollup import aggregate, overall_status_for_regions, status_code_for
from api.services.state import describe as describe_svc
from api.services.state import ksvc_state, ownership
from api.services.state import metrics as metrics_svc
from api.services.state import summaries as summaries_svc
from api.services.state.ksvc_state import ISRAEL_TZ, ksvc_failure_message, revision_failure_message
from api.services.state.ownership import hidden_404
from api.services.streams import logs as logs_stream
from api.services.streams import pods as pods_stream
from api.services.streams import stats as stats_stream
from api.services.streams.capacity import StreamCapacity
from api.services.streams.sse import StreamEvent
from api.services.tenant_namespace import provision_namespace
from api.services.workloads.request import ApplyRequest
from api.services.workloads.stream_guard import _slot_guarded
from common.build import BuildBackend, BuildPlan
from common.cluster import NamespacedCluster, ResourceKind
from common.config import RegistryConfig
from common.errors import (
    ForbiddenError,
    NotFoundError,
    RegionTotalFailure,
    ServiceUnavailableError,
)

logger = get_logger(__name__)


async def run_background(fn, *args) -> None:
    """Run background work, logging (not raising) any failure.

    Failures surface to the client via status polling, not the caller.

    Args:
        fn: The coroutine function to run (e.g. create/update).
        *args: Positional arguments passed to ``fn``.
    """
    try:
        await fn(*args)
    except Exception:  # noqa: BLE001 - background work; surfaced via status polling
        # The plain-string args only (group, name), never the whole tuple: the
        # spec in there carries the caller's git/registry tokens and secret
        # values, which must not reach a log line.
        ident = "/".join(a for a in args if isinstance(a, str)) or "?"
        logger.exception("background %s failed for %s", getattr(fn, "__name__", fn), ident)


async def _retag_region(cluster: NamespacedCluster, manifests: Sequence[dict]) -> None:
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
        logger.info("Image '%s' re-tagged from '%s' to '%s' in %s", name, had, want, cluster.region)
        return had

    try:
        previous = await asyncio.to_thread(retag)
    except Exception:  # noqa: BLE001 - the apply below reports the real failure
        logger.exception("could not re-tag Image '%s' in %s", name, cluster.region)
        return
    if previous:
        # This region's registry: a reference on another host is somebody
        # else's repository, and reclaim already refuses to touch it.
        await asyncio.to_thread(registry_svc.reclaim_moved_repositories, cluster.registry, previous)


def _pod_authorizer(cluster: NamespacedCluster, name: str, pod: str, kind: str, user: Principal):
    """The check both log reads run, as one blocking callable.

    Two checks, run by the log stream and the log snapshot alike: the caller
    must own the workload, and the named pod must carry that workload's service
    label. Owning the workload is not owning every pod in its namespace
    (docs/STREAMING.md - Authorizing a pod).

    Args:
        cluster: The local region.
        name: The workload name (and its KSVC's).
        pod: The pod the caller named.
        kind: The offering label ("function"/"container").
        user: The authenticated caller.

    Returns:
        A callable returning the pod's revision, to run off the event loop.
    """

    def authorize() -> str | None:
        obj = cluster.get(ResourceKind.KNATIVE_SERVICE, name)
        if not ownership.owned_by(obj, user, kind):
            raise hidden_404("read logs of", kind, name, user, obj)
        found = cluster.get(ResourceKind.POD, pod)
        labels = (found.get("metadata", {}) or {}).get("labels", {}) or {}
        if labels.get(pods_stream.SERVICE_LABEL) != name:
            # Someone else's pod, or none of ours. Same answer as absent: the
            # response must not confirm that a pod by this name exists.
            logger.debug("pod '%s' is not a pod of %s '%s'; hidden as 404", pod, kind, name)
            raise NotFoundError(f"pod '{pod}' not found")
        return labels.get(pods_stream.REVISION_LABEL)

    return authorize


def _roster_reader(cluster: NamespacedCluster, name: str, kind: str, user: Principal, verb: str):
    """The read both roster endpoints run, as one blocking callable.

    Authorizes against the workload, then lists its pods - the same two reads
    whether the roster is streamed or read once.

    Args:
        cluster: The local region.
        name: The workload name (and its KSVC's).
        kind: The offering label ("function"/"container").
        user: The authenticated caller.
        verb: What the caller was doing, for the hidden-404 log line.

    Returns:
        A callable returning the roster, to run off the event loop.
    """

    def read() -> list:
        obj = cluster.get(ResourceKind.KNATIVE_SERVICE, name)
        if not ownership.owned_by(obj, user, kind):
            raise hidden_404(verb, kind, name, user, obj)
        return pods_stream.read_roster(cluster, name)

    return read


@dataclass(frozen=True)
class _Composed:
    """The manifests one apply writes, composed once for every target region.

    Attributes:
        ksvc_for: The KSVC for a region, as a function of the region name; only
            the image varies between regions.
        backing: The derived objects the KSVC references (config, files, the
            offering's secrets), identical everywhere.
        mapping: The DomainMapping.
        to_prune: The derived objects an earlier spec owned and this one no
            longer references.
    """

    ksvc_for: Callable[[str], dict]
    backing: list[dict]
    mapping: dict
    to_prune: set[tuple[ResourceKind, str]]


def _to_prune(
    req: ApplyRequest, offering: Offering, backing: Sequence[dict]
) -> set[tuple[ResourceKind, str]]:
    """The derived objects the new spec no longer references.

    The resolvers emit only what the new spec still needs, so the rest is
    pruned: dropping the last secret env var must delete its Secret, not orphan
    it. Nothing on a create - there is no earlier spec to prune.

    Args:
        req: The apply request.
        offering: The offering, for the secrets it manages.
        backing: The derived manifests this apply writes.

    Returns:
        ``(kind, name)`` pairs to delete before the apply.
    """
    if req.created:
        return set()
    applied = {(ResourceKind.from_kind(m["kind"]), m["metadata"]["name"]) for m in backing}
    # Keyed on whether the KSVC still references it, not on the manifest: the
    # secret can be carried forward without being re-applied.
    if req.pull_secret_name:
        applied.add((ResourceKind.SECRET, req.pull_secret_name))
    managed = {
        (ResourceKind.SECRET, env_secret_name(req.name)),
        (ResourceKind.CONFIG_MAP, files_name(req.name)),
        (ResourceKind.SECRET, files_name(req.name)),
    } | offering.managed_secrets(req.name)
    return managed - applied


@dataclass(frozen=True)
class _RegionRead:
    """What one region's fetch found, beside the status row it produced.

    A fetch returns one of these for a region that has the workload, and the
    fan-out collects them in target order. A region that timed out or raised
    yields the fan-out's own RegionStatus and no read, so a list of these holds
    only the regions that answered.

    Attributes:
        status: The region's row.
        obj: The KSVC read there.
        cluster: The region it was read from, for whatever is read next.
        build: The region's build state, when the offering has one.
        usage: The region's live usage, when the caller asked for it.
    """

    status: RegionStatus
    obj: dict
    cluster: NamespacedCluster
    build: BuildStatusView | None = None
    usage: region_read.RegionUsage | None = None


def _split(
    results: Sequence[RegionStatus | _RegionRead | None],
) -> tuple[list[RegionStatus], list[_RegionRead]]:
    """Separate a fan-out's rows from the reads that carry an object.

    A None is a region the workload is not deployed to, and belongs in
    neither. A bare RegionStatus is a region that produced a row and nothing
    else - the fan-out's own Timeout/Failed, or a fetch's Absent.

    Args:
        results: The fan-out's results, one per target.

    Returns:
        Every row (a read's row included), and the reads.
    """
    statuses: list[RegionStatus] = []
    reads: list[_RegionRead] = []
    for result in results:
        if result is None:
            continue
        if isinstance(result, _RegionRead):
            reads.append(result)
            statuses.append(result.status)
        else:
            statuses.append(result)
    return statuses, reads


def _stamp_commit(cluster: NamespacedCluster, name: str, commit: str | None) -> bool:
    """Record - or clear - the commit a git push pinned, on one region's KSVC.

    ``metadata.annotations`` only, not the template's: a pin says which *source*
    to compile next, not which image to run, so it must cut no Knative revision.
    Clearing writes an explicit ``null``, since the annotation is set by a merge
    patch and an apply that merely omits the key may not remove it.

    Args:
        cluster: The region to write to.
        name: The workload name.
        commit: The commit to pin, or None to clear.

    Returns:
        True if the workload is here and was stamped; False if it does not run
        here - not a failure, since the build apply skips it too.
    """
    patch = {"metadata": {"annotations": {ANNOTATION_GIT_COMMIT: commit}}}
    try:
        cluster.patch(ResourceKind.KNATIVE_SERVICE, name, patch)
    except NotFoundError:
        return False
    return True


def _assert_any_region_wrote(statuses: Sequence[RegionStatus], what: str) -> None:
    """Fail the request when a fan-out write landed in no region at all.

    ``fanout`` records a region's failure on its status instead of raising, so a
    write whose result is discarded reports success however many regions
    refused it. Absent is not a failure - the workload does not run there.

    Args:
        statuses: The per-region results.
        what: What was being written, for the error.

    Raises:
        RegionTotalFailure: If every region errored.
    """
    if statuses and all(s.message is not None for s in statuses):
        raise RegionTotalFailure(
            f"Could not {what} in any region.",
            details=[{"region": s.region, "message": s.message} for s in statuses],
        )


def _annotation(reads: Sequence[_RegionRead], key: str) -> str | None:
    """The first region's value for a KSVC annotation, or None if none carries it.

    The spec is uniform across regions, so the first copy that has it will do.
    """
    for read in reads:
        annotations = (read.obj.get("metadata", {}) or {}).get("annotations", {}) or {}
        if key in annotations:
            return annotations[key]
    return None


def _live_status(cluster: NamespacedCluster, obj: dict) -> RegionStatus:
    """One region's row for a deployed workload, from its KSVC and its Revision.

    Reads the Revision as well as the KSVC: the Revision carries the live scale
    and the specific rollout-failure condition, and on a Failed status its
    conditions are preferred over the KSVC's, so the row carries a cause rather
    than a bare status=Failed.

    Args:
        cluster: The region the KSVC was read from.
        obj: The KSVC.

    Returns:
        The row.
    """
    status, revision = ksvc_state.ksvc_status(obj)
    rev = region_read.revision(cluster, revision)
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
        replicas=ksvc_state.revision_replicas(rev),
    )


@dataclass(frozen=True)
class _Rollup:
    """The per-region rows folded with their builds, and what the fold decided.

    Attributes:
        statuses: The rows, each carrying its region's build where one runs.
        overall: The headline status.
        build: The builds rolled up, or None when no region has one.
        reason: The headline's machine-readable cause, if any.
    """

    statuses: list[RegionStatus]
    overall: str
    build: BuildStatusView | None
    reason: str | None


def _roll_up(offering: Offering, statuses: list[RegionStatus], reads: list[_RegionRead]) -> _Rollup:
    """Fold each region's build into its row and into the headline.

    Build-first, per region and then rolled up: while a region is building, its
    KSVC cannot pull an image that does not exist there yet, so the build state
    takes precedence over the KSVC's (docs/FUNCTIONS.md - Function Status
    Resolution). Called by both the full GET and the stats poll. A down region
    counts as Failed.

    The headline reason is Kubernetes-style: a failed build is the reason
    wherever the rows stand (regions still serving their previous revision stay
    Ready), else the first recognized per-region cause.

    Args:
        offering: The offering, for whether there is a build to fold at all.
        statuses: Every region's row, the unreachable ones included.
        reads: The regions that answered, with their builds.

    Returns:
        The fold.
    """
    overall = overall_status_for_regions(statuses)
    builds = {r.status.region: r.build for r in reads} if offering.has_build else {}
    build = ksvc_state.roll_up_builds(list(builds.values()))
    statuses = ksvc_state.regions_with_build_status(statuses, builds)
    overall = ksvc_state.with_build_status(overall, build)
    if build is not None and build.state == "Failed":
        reason = "BuildFailed"
    else:
        reason = next((s.reason for s in statuses if s.reason), None)
    return _Rollup(statuses=statuses, overall=overall, build=build, reason=reason)


def _stats_response(
    rollup: _Rollup, usage_by_region: Mapping[str, region_read.RegionUsage | None]
) -> WorkloadStatsResponse:
    """The poll view: the rollup, and per-region replicas and usage with totals.

    Both totals are null rather than partial when a region could not answer.

    Args:
        rollup: The folded rows and headline.
        usage_by_region: Each answering region's raw usage read. Raw: the total
            is summed from these, not from the rounded figures the response
            carries.

    Returns:
        The response body.
    """
    statuses = rollup.statuses
    reads = [usage_by_region.get(s.region) for s in statuses]
    usage = None
    if all(r is not None and r.measured for r in reads):
        usage = metrics_svc.total(r.total for r in reads if r.total)
    replicas = None
    if all(s.replicas is not None for s in statuses):
        replicas = sum(s.replicas for s in statuses)
    return WorkloadStatsResponse(
        status=rollup.overall,  # type: ignore[arg-type]
        reason=rollup.reason,
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
            capacity: The stream pool and admission gate. None builds one from
                ``settings`` on the first streaming use.
        """
        self.settings = settings
        self.deployer = deployer
        self.builder = builder
        self._capacity = capacity

    @property
    def capacity(self) -> StreamCapacity:
        """The stream pool; the fallback is built on first streaming use.

        An engine constructed without one builds it from ``settings`` here, so
        the thread pool exists only once something streams
        (docs/STREAMING.md - A held-open stream holds a thread).
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

    def namespace_for(self, group: str) -> str:
        """The namespace this group's workloads live in.

        The single resolution point: every read and every write in the engine
        names its namespace through this method.

        Args:
            group: The owning (normalized) group.

        Returns:
            ``{group}{suffix}``.

        Raises:
            ValidationError: If the group cannot name a namespace - too long
                with the suffix, or reserved. Both are refused at accept time,
                not discovered by the tenant controller later.
        """
        return self.settings.tenant_namespaces.namespace_for(group)

    def targets_for(self, group: str) -> list[NamespacedCluster]:
        """The clusters a request for ``group`` fans out to, namespace-bound.

        Every configured region, always: placement is not a client choice, so a
        create and an update reach the same set (docs/ARCHITECTURE.md - Region
        selection).

        Args:
            group: The owning (normalized) group.

        Returns:
            One bound view per configured region.
        """
        return self.deployer.resolve_targets(None, self.namespace_for(group))

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
        targets: list[NamespacedCluster],
        *,
        host: str | None = None,
        require_absent: bool = False,
    ) -> None:
        """Assert a workload can be deployed: host free, and optionally name unused.

        Pure cluster probes - see
        :func:`api.services.regions.preflight.assert_deployable`. The namespace
        itself is provisioned separately, once per accepted request
        (:meth:`provision_namespace`), so the background re-check before the
        mutation does not repeat that round trip.
        """
        await preflight.assert_deployable(
            self.deployer, name, group, targets, host=host, require_absent=require_absent
        )

    async def provision_namespace(self, group: str) -> None:
        """Have the tenant controller provision the group's namespace.

        Called once per accepted create *and* update: an update's namespace
        normally exists, but a region added after the group's first create
        would never get one otherwise - and a provision of an up-to-date
        namespace costs the controller one read per region.

        The call lives here rather than inside ``preflight`` because that
        module is pure cluster probes - putting an HTTP client in it would
        drag a dependency into every test that only wanted to check a host.
        """
        await provision_namespace(
            group, self.settings.tenant_namespaces, verify=self.settings.ca_bundle.file
        )

    async def assert_host_available(
        self, host: str, name: str, group: str, targets: list[NamespacedCluster]
    ) -> None:
        """Assert ``host`` is free (see :meth:`assert_deployable`)."""
        await self.assert_deployable(name, group, targets, host=host)

    async def assert_workload_absent(
        self, name: str, group: str, targets: list[NamespacedCluster]
    ) -> None:
        """Assert ``name`` is unused in the group's namespace (see :meth:`assert_deployable`)."""
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
            statusUrl=f"{api_base(self.settings)}/groups/{group}/{offering.name}s/{name}",
            **extra,
        )

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
            spec: The create request (carries name/hostname/env/files).
            user: The authenticated caller.
            background: FastAPI background tasks to schedule the deploy on.
            work: The offering's background create coroutine, run as
                ``work(group, spec, user)``.
            **extra: Offering-specific fields echoed onto the accepted body.

        Returns:
            A Pending response with a ``statusUrl`` to poll.
        """
        self.assert_group(user, group)
        targets = self.targets_for(group)
        host = self.host_for(spec.name, spec.hostname, group)
        # Validate synchronously (400) before the 202, so bad input never reaches the
        # background deploy.
        self.validate_spec(spec.name, group, user.username, spec.env, spec.files)
        # The namespace first - a name cannot be proven free in a namespace
        # that does not exist yet - then host and name in one pass: one round
        # trip answers both, and a conflict is an immediate 409 rather than a
        # background failure.
        await self.provision_namespace(group)
        await self.assert_deployable(spec.name, group, targets, host=host, require_absent=True)
        background.add_task(run_background, work, group, spec, user)
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
        # Provisioned on update too: normally a cheap no-op, but a region added
        # after the group's first create gets its namespace exactly here. Best
        # effort, unlike a create's: the workload just loaded above proves the
        # namespace exists, and a controller outage must not block an update -
        # or a rollback - of something already running. A refusal (4xx) still
        # propagates: that is config, not outage.
        try:
            await self.provision_namespace(group)
        except ServiceUnavailableError as exc:
            logger.warning(
                "updating '%s' without provisioning for group '%s': %s", name, group, exc
            )
        # A host collision must be a synchronous 409, not a lost background failure.
        # The workload's own mapping counts as available.
        await self.assert_host_available(host, name, group, self.targets_for(group))
        background.add_task(run_background, work, group, name, spec, user, existing)
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
        targets = self.targets_for(req.group)
        host = self.host_for(req.name, req.hostname, req.group)

        # Re-checked here, immediately before the mutation, and not trusted from
        # accept time: the DomainMapping name IS the host, so an idempotent apply
        # would otherwise take over another workload's mapping. On a create the
        # same pass also confirms the name is still unused, so the offering
        # services do not probe for that separately.
        await self.assert_deployable(
            req.name, req.group, targets, host=host, require_absent=req.created
        )
        composed = self._compose(req, offering, host)
        # Before any apply: a moved tag cannot be applied over, only replaced.
        await self.retag_build(targets, req.region_resources)

        def apply(cluster: NamespacedCluster) -> RegionStatus:
            # A region builds what it runs, so its build objects ride along with
            # its KSVC and are owned by it.
            return region_apply.apply_to_region(
                cluster,
                name=req.name,
                ksvc=composed.ksvc_for(cluster.region),
                backing=composed.backing + list(req.region_resources.get(cluster.region, ())),
                pull_secret_manifest=req.pull_secret_manifest,
                mapping=composed.mapping,
                to_prune=composed.to_prune,
                created=req.created,
                prev_host=req.prev_host,
            )

        statuses = await self.deployer.fanout(targets, apply)
        return self._applied(req, offering, host, statuses)

    def _compose(self, req: ApplyRequest, offering: Offering, host: str) -> _Composed:
        """Compose everything one apply writes, once, for every target region.

        Args:
            req: The apply request.
            offering: The offering being deployed.
            host: The resolved external host.

        Returns:
            The manifests, and what to prune before them.
        """
        owner = req.user.username
        resolved = resolve_files(req.name, req.group, owner, req.files, req.kept_files)
        resolved_env = resolve_env(req.name, req.group, owner, req.env, req.kept_env)
        backing = resolved.backing + resolved_env.backing + list(req.extra_secrets)

        def ksvc_for(region: str) -> dict:
            """Compose the KSVC for one region. Only the image varies."""
            return ksvc_svc.build_ksvc(
                name=req.name,
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
                revision=req.revision,
                commit=req.commit,
                path=req.path,
                ca_config_map=self.settings.ca_bundle.config_map,
                ca_mount_path=self.settings.ca_bundle.mount_path,
                ca_file=self.settings.ca_bundle.file,
                pull_stamp=req.pull_stamp,
            )

        mapping = route_svc.build_domain_mapping(
            name=req.name, group=req.group, owner=owner, offering=offering.name, host=host
        )
        return _Composed(
            ksvc_for=ksvc_for,
            backing=backing,
            mapping=mapping,
            to_prune=_to_prune(req, offering, backing),
        )

    @staticmethod
    def _applied(
        req: ApplyRequest, offering: Offering, host: str, statuses: list[RegionStatus]
    ) -> tuple[WorkloadResponse, int]:
        """The response for an apply, from what was submitted and how it landed.

        Args:
            req: The apply request.
            offering: The offering deployed.
            host: The resolved external host.
            statuses: One row per target region.

        Returns:
            The response body and HTTP status code.
        """
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

    def target_registries(self) -> dict[str, RegistryConfig]:
        """The registry each configured region builds into, keyed by region name.

        What a build plan needs, read off the resolved clusters, so a region's
        registry has one source. Every region, matching :meth:`targets_for`: a
        function builds wherever it runs.

        Returns:
            ``{region: registry}`` for every configured region.
        """
        return {c.region: c.registry for c in self.deployer.clusters()}

    async def apply_build(
        self,
        name: str,
        group: str,
        plan: BuildPlan,
        *,
        trigger: bool = True,
        commit: str | None = None,
        had_commit: bool = False,
    ) -> bool:
        """Re-declare a workload's build in every region that runs it, then ask for one.

        The rebuild path, and the one write in the engine that leaves the KSVC
        alone: the workload's desired state does not change, so only the build
        objects and the trigger are written (docs/FUNCTIONS.md - Building again
        without changing anything).

        Applies before triggering, per region: a region that has never built the
        function gets its Image created here and starts building from that, and
        :meth:`~common.build.BuildBackend.trigger` finds nothing to annotate and
        reports so.

        Args:
            name: The workload name.
            group: The owning group.
            plan: The build plan to apply (its git Secret included, so the region
                that builds can always clone).
            trigger: Ask kpack for one more build of an unchanged spec. False
                where the applied spec is itself the change it builds from - a
                push pinning a commit, or a rebuild clearing one - since a
                trigger too would make a second build (docs/BUILDING.md -
                Convergence rules).
            commit: The commit a push pinned, stamped so later reconstructions
                carry it. None *clears* any stored pin.
            had_commit: Whether a pin was stored. Only skips the annotation
                write when there is neither one to set nor one to clear.

        Returns:
            True if an existing build was triggered in any region; False if
            applying the plan is itself what starts them.
        """
        targets = self.targets_for(group)
        await self.retag_build(targets, plan.manifests_by_region)
        # The KSVC is the replicated source of truth and the Image is derived
        # from it, so the pin lands first: a patch surviving a failed apply is
        # re-composed by the next write, where the reverse leaves a build
        # nobody can reconstruct.
        stamp = commit is not None or had_commit

        def work(cluster: NamespacedCluster) -> RegionStatus:
            manifests = list(plan.replicated) + plan.manifests_for(cluster.region)
            if stamp and not _stamp_commit(cluster, name, commit):
                return RegionStatus(region=cluster.region, status="Absent")
            # Skips a region the workload does not run in, which is also every
            # region the plan does not cover.
            if not region_apply.apply_build_objects(cluster, manifests, name=name):
                return RegionStatus(region=cluster.region, status="Absent")
            triggered = trigger and self.builder.trigger(cluster, name, group)
            return RegionStatus(
                region=cluster.region, status="Building" if triggered else "Pending"
            )

        statuses = await self.deployer.fanout(targets, work)
        return any(s.status == "Building" for s in statuses)

    async def clear_commit(self, name: str, group: str) -> None:
        """Remove the pinned commit from the workload, in every region.

        See :func:`_stamp_commit` for why this is an explicit ``null``.

        Args:
            name: The workload name.
            group: The owning group.
        """

        def work(cluster: NamespacedCluster) -> RegionStatus:
            found = _stamp_commit(cluster, name, None)
            return RegionStatus(region=cluster.region, status="Ready" if found else "Absent")

        statuses = await self.deployer.fanout(self.targets_for(group), work)
        # A pin left behind is read back by the next build and reported on the
        # next GET, so a silent failure here outlives the request.
        _assert_any_region_wrote(statuses, f"clear the pinned commit on '{name}'")

    async def apply_owned_secret(self, name: str, group: str, manifest: dict) -> None:
        """Apply one owned Secret to every region the workload runs in.

        The write behind a credential rotation: no build declared and no KSVC
        composed, so nothing deploys and no revision is cut. Owner-stamped so it
        cascades on delete; a region without the workload is skipped.

        Args:
            name: The workload name, whose KSVC owns the Secret.
            group: The owning group.
            manifest: The Secret to apply.

        Raises:
            RegionTotalFailure: If no region could be written.
        """

        def work(cluster: NamespacedCluster) -> RegionStatus:
            if not region_apply.apply_build_objects(cluster, [manifest], name=name):
                return RegionStatus(region=cluster.region, status="Absent")
            return RegionStatus(region=cluster.region, status="Ready")

        statuses = await self.deployer.fanout(self.targets_for(group), work)
        # `fanout` turns a region's error into a status rather than raising, so
        # a caller that drops the statuses reports success for a write that
        # landed nowhere. For a rotation that is the worst answer available: the
        # caller reconfigures the hook with a token no region will accept, while
        # the one they replaced stays live.
        _assert_any_region_wrote(statuses, f"rotate the webhook token for '{name}'")

    async def retag_build(
        self, targets: list[NamespacedCluster], region_resources: Mapping[str, Sequence[dict]]
    ) -> None:
        """Make way for an Image whose tag has moved, and reclaim what it left.

        ``spec.tag`` is **immutable** on a kpack Image, so applying a moved one is
        rejected at admission. The Image is deleted instead and the apply that
        follows recreates it; a new Image with no prior Build builds on its own,
        so nothing else has to ask for one.

        Per region, because each region's tag moves independently
        (docs/RUNTIMES.md - Moving a function's repository).

        The tag is compared first, so this is one GET per region and no write at
        all unless a tag has actually moved.

        Args:
            targets: The clusters being written to.
            region_resources: Each region's build manifests; its Image is picked out.
        """
        if not region_resources:
            return
        await asyncio.gather(
            *(_retag_region(c, region_resources.get(c.region, ())) for c in targets)
        )

    async def stamp_pull(self, name: str, group: str, stamp: str) -> list[RegionStatus]:
        """Stamp a new pull marker on the workload in every region.

        Changing a ``spec.template`` annotation is what makes Knative cut a
        revision and resolve the tag again. A merge patch, not a full apply,
        like the rebuild trigger: no desired state changes
        (docs/CONTAINERS.md - Pulling the tag again).

        Args:
            name: The workload name.
            group: The owning group.
            stamp: The value to write, the same in every region so they cannot
                drift onto different revisions.

        Returns:
            One status per region; ``Absent`` where the workload does not run.
        """
        patch = {
            "metadata": {"annotations": {ANNOTATION_PULL_STAMP: stamp}},
            "spec": {"template": {"metadata": {"annotations": {ANNOTATION_PULL_STAMP: stamp}}}},
        }

        def stamp_region(cluster: NamespacedCluster) -> RegionStatus:
            try:
                cluster.patch(ResourceKind.KNATIVE_SERVICE, name, patch)
            except NotFoundError:
                return RegionStatus(region=cluster.region, status="Absent")
            return RegionStatus(region=cluster.region, status="Deploying")

        targets = self.targets_for(group)
        return await self.deployer.fanout(targets, stamp_region)

    def _representative(self, reads: list[_RegionRead]) -> _RegionRead:
        """The read to take the uniform spec from: the local region's, else any.

        The spec is uniform across regions (active/active), so any copy will
        do; the local one is the cheapest hop for whatever is read from it next.

        Args:
            reads: The regions that have the workload; must not be empty.

        Returns:
            The representative read.
        """
        local = self.deployer.local_region()
        return next((r for r in reads if r.status.region == local), reads[0])

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
        targets = self.targets_for(group)

        def fetch(cluster: NamespacedCluster) -> RegionStatus | _RegionRead:
            # Only a real 404 means absent; anything else must propagate so a down region
            # is recorded as an error, not mistaken for absence.
            try:
                obj = cluster.get(ResourceKind.KNATIVE_SERVICE, name)
            except NotFoundError:
                return RegionStatus(region=cluster.region, status="Absent")
            return _RegionRead(
                status=RegionStatus(region=cluster.region, status="Present"),
                obj=obj,
                cluster=cluster,
            )

        statuses, reads = _split(await self.deployer.fanout(targets, fetch))
        if not reads:
            # Absent on every reachable region. An unreachable region cannot prove
            # absence, so that case fails closed with a 503 instead of a 404.
            preflight.assert_all_regions_checked(statuses, f"load workload '{name}'")
            raise NotFoundError(f"{offering.name} workload '{name}' not found")

        rep = self._representative(reads)
        # The name could belong to the other offering, or - if labels ever
        # drifted - another owner; both mean "not this workload" -> hide as 404.
        if not ownership.owned_by(rep.obj, user, offering.name):
            raise NotFoundError(f"{offering.name} workload '{name}' not found")
        # The image is the exception to the uniform spec - each region runs what
        # its own build pushed - so it is kept per region, and an update carries
        # each one forward untouched.
        images = {
            r.status.region: image for r in reads if (image := ksvc_state.extract_image(r.obj))
        }
        # Reading the backing Secrets is blocking cluster I/O; run it in a thread
        # so it doesn't stall the event loop (as get()/describe_spec do).
        state = await asyncio.to_thread(
            region_read.existing_state, rep.obj, rep.cluster, offering, name
        )
        return {**state, "images": images}

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

        def fetch(cluster: NamespacedCluster) -> _RegionRead | None:
            # A 404 means not deployed here, so omit the region rather than fail it.
            # Anything else propagates, keeping a down region visible as Failed.
            try:
                obj = cluster.get(ResourceKind.KNATIVE_SERVICE, name)
            except NotFoundError:
                return None
            # The build is NOT uniform: each region builds its own copy, so its
            # state is read in the same per-region thread as that region's KSVC.
            # No usage read: live usage is a cluster call of its own, and only
            # /stats reports it.
            build = None
            if offering.has_build:
                build = offering.build_status(self.builder, cluster, name, group)
            return _RegionRead(
                status=_live_status(cluster, obj), obj=obj, cluster=cluster, build=build
            )

        targets = self.targets_for(group)
        statuses, reads = _split(await self.deployer.fanout(targets, fetch, read=True))
        if not reads:
            # Present on no reachable region. An unreachable region cannot prove
            # absence, so that case is a 503; otherwise it is genuinely gone -> 404.
            preflight.assert_all_regions_checked(statuses, f"get workload '{name}'")
            raise NotFoundError(f"{kind} '{name}' not found")

        # The spec is uniform across regions: read it (and authorize) from one
        # representative copy.
        rep = self._representative(reads)
        if not ownership.owned_by(rep.obj, user, kind):
            raise hidden_404("get", kind, name, user, rep.obj)

        rollup = _roll_up(offering, statuses, reads)
        host = _annotation(reads, ANNOTATION_HOST)
        if host is None:
            host = route_svc.host_for(name, group, self.settings.route_domain)

        # The offering's own extras (a function's webhook token) ride in the
        # same thread as the spec read: a second `run_read` would be another
        # round trip and another pool admission, and a saturated pool there
        # would 503 a GET that had already succeeded.
        def read_spec(cluster: NamespacedCluster, obj: dict):
            return (
                region_read.describe_spec(cluster, obj),
                offering.read_response_extras(cluster, name, group, self.settings),
            )

        spec, extras = await self.deployer.run_read(read_spec, rep.cluster, rep.obj)
        common = dict(
            name=name,
            group=group,
            type=kind,
            hostname=host,
            status=rollup.overall,
            reason=rollup.reason,
            size=_annotation(reads, ANNOTATION_SIZE),
            createdAt=ksvc_state.creation_time(rep.obj),
            regions=rollup.statuses,
            scaling=spec.scaling,
            env=spec.env,
            files=spec.files,
            **extras,
        )
        return offering.fetched_response(common, rep.obj, spec, rollup.build)

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
        rollup, but none of the desired-state reads: no file ConfigMaps and no
        backing Secret, so a refresh reads no secret material out of the cluster
        (docs/FUNCTIONS.md - Polling live state).

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
                The stats stream passes the streaming pool's executor, so its
                repeated readings run there and not on the request threads
                (docs/STREAMING.md - A held-open stream holds a thread).

        Returns:
            The live stats view.

        Raises:
            NotFoundError: If the workload exists on no reachable region.
            ServiceUnavailableError: If it can't be confirmed absent because a
                region was unreachable.
        """
        self.assert_group(user, group)
        kind = offering.name  # the API kind ("function"/"container") is the offering label

        def fetch(cluster: NamespacedCluster) -> _RegionRead | None:
            try:
                obj = cluster.get(ResourceKind.KNATIVE_SERVICE, name)
            except NotFoundError:
                return None  # not deployed here; omit the region rather than fail it
            # Read per region inside the fan-out, so a stats *stream* reads the
            # build on its own pool, like every other read here.
            build = None
            if offering.has_build:
                build = offering.build_status(self.builder, cluster, name, group)
            return _RegionRead(
                status=_live_status(cluster, obj),
                obj=obj,
                cluster=cluster,
                build=build,
                usage=region_read.region_usage(cluster, name),
            )

        targets = self.targets_for(group)
        results = await self.deployer.fanout(targets, fetch, executor=executor, read=True)
        statuses, reads = _split(results)
        if not reads:
            # An unreachable region cannot prove absence, so that case is a 503.
            preflight.assert_all_regions_checked(statuses, f"get stats of workload '{name}'")
            raise NotFoundError(f"{kind} '{name}' not found")

        rep = self._representative(reads)
        if not ownership.owned_by(rep.obj, user, kind):
            raise hidden_404("stats of", kind, name, user, rep.obj)

        return _stats_response(
            _roll_up(offering, statuses, reads), {r.status.region: r.usage for r in reads}
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
        denied: list[str] = []

        def remove(cluster: NamespacedCluster) -> RegionStatus:
            # A clean 404 means "not deployed here", which is not a failure; only
            # a region that cannot answer at all is an error.
            try:
                obj = cluster.get(ResourceKind.KNATIVE_SERVICE, name)
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
                cluster.delete(ResourceKind.KNATIVE_SERVICE, name)
            except NotFoundError:
                return RegionStatus(region=cluster.region, status="Absent")  # raced a peer
            return RegionStatus(region=cluster.region, status="Deleted")

        targets = self.targets_for(group)
        statuses = await self.deployer.fanout(targets, remove)

        # An unreachable region cannot confirm the workload is gone, so this fails
        # closed with a 503. Delete is idempotent, so the caller's retry is a no-op
        # over the regions that did succeed.
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
        # and whatever the ownerReferences did not cascade to can go too. Reached even
        # when nothing was deleted here, which is the case where an earlier partial
        # delete left those resources behind.
        # Per region, because what is left behind is per region: each built its own
        # image, into its own registry, from its own build objects
        # (docs/BUILDING.md - Registry cleanup on delete).
        await asyncio.gather(
            *(
                asyncio.to_thread(
                    offering.after_delete,
                    DeleteContext(cluster=cluster, name=name, group=group),
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

        Feeds per-pod log streaming: a pod name is what ``stream_pod_logs`` takes,
        and nothing else in the API returns one.

        Local region only, matching the log streams it feeds - a pod name is only
        useful where its log can be read.

        The first roster is read here, not inside the stream: it is also what
        authorizes the request, so a workload that does not exist is a 404 with an
        envelope rather than a stream that opens and immediately errors
        (docs/STREAMING.md - Errors after the first byte).

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
        cluster = self.deployer.local_cluster(self.namespace_for(group))
        read = _roster_reader(cluster, name, kind, user, "stream pods of")

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
                workload=name,
                interval=self.capacity.interval(interval),
            ),
        )

    async def pods(self, offering: Offering, name: str, user: Principal, group: str) -> PodRoster:
        """The workload's pods on the local region, read once (``follow=false``).

        The non-streaming half of :meth:`stream_pods`, and the same reads, for a
        caller that cannot hold a connection open (docs/STREAMING.md -
        follow=false).

        No stream slot: this is an ordinary bounded request that ends, and it runs
        on the read pool like every other request.

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
        cluster = self.deployer.local_cluster(self.namespace_for(group))
        read = _roster_reader(cluster, name, kind, user, "read pods of")

        return PodRoster(
            name=name,
            group=group,
            type=kind,  # type: ignore[arg-type]
            region=self.deployer.local_region(),
            # The read pool, like every page read: the console's non-streaming
            # fallback polls this, and the default executor has no admission.
            pods=await self.deployer.run_read(read),
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
        authorization, so the pod-ownership rule is one rule. What it returns is
        bounded by what the node still holds - there is no history behind that,
        whichever way it is read - and further by the configured snapshot bounds:
        the newest ``snapshot_tail_lines`` lines, within ``snapshot_max_bytes``.
        A caller's own bounds are clamped to those, never widened
        (docs/STREAMING.md - follow=false).

        No stream slot: the read is an ordinary bounded request that ends.

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
        cluster = self.deployer.local_cluster(self.namespace_for(group))
        authorize = _pod_authorizer(cluster, name, pod, kind, user)

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

        # The read pool, like every page read: the console's non-streaming
        # fallback polls this, and the default executor has no admission.
        revision, lines = await self.deployer.run_read(read)
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
    ) -> AsyncIterator[StreamEvent | str]:
        """Follow one of the workload's pods' logs, on the local region.

        Local region only: Kubernetes keeps no log buffer beyond the node that
        wrote it, so there is nowhere else to read from.

        The pod is authorized twice: the caller must own the workload, and the
        named pod must carry this workload's service label, since owning the
        workload is not owning every pod in its namespace. A pod that is not
        this workload's is a 404, identical to one that does not exist
        (docs/STREAMING.md - Authorizing a pod).

        Args:
            offering: The offering being read.
            name: Workload name.
            user: The authenticated caller.
            group: Owning group.
            pod: The pod to follow, as ``stream_pods`` named it.
            container: The pod container to read.
            since_seconds: How far back the log starts; None begins at the
                moment of connection.
            tail_lines: Start at the newest this-many lines instead, however old
                they are. Clamped to the configured snapshot tail bound, which
                is the same "history a client gets at once".

        Returns:
            The event stream, beginning with an ``open`` event.

        Raises:
            NotFoundError: If the workload or the pod isn't here, the pod is not
                this workload's, or the caller can't access it (all hidden as 404).
            ServiceUnavailableError: If no stream slot is free.
        """
        self.assert_group(user, group)
        kind = offering.name  # the API kind ("function"/"container") is the offering label
        cluster = self.deployer.local_cluster(self.namespace_for(group))
        authorize = _pod_authorizer(cluster, name, pod, kind, user)

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

        The first reading is taken here, not inside the stream: it also
        authorizes the request, so a workload that does not exist is a 404 with
        an envelope rather than a stream that opens and immediately errors
        (docs/STREAMING.md - Errors after the first byte).

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
        cannot pull its image yet, so the build state takes precedence. The build
        states come from one label-selected read per region, so the fold costs a
        single round trip for the entire listing, taken inside the fan-out rather
        than chained onto it.

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

        def fetch(cluster: NamespacedCluster) -> tuple[list[dict], dict]:
            # Both reads in one per-region thread: the build states belong to this
            # region, so pairing them costs no extra round trip and cannot
            # attribute one region's builds to another. Branches on the declared
            # capability rather than on the offering name; an offering with no
            # build skips the read and returns {}.
            ksvcs = cluster.get(ResourceKind.KNATIVE_SERVICE, label_selector=selector)
            if not offering.has_build:
                return ksvcs, {}
            return ksvcs, offering.build_states(self.builder, cluster, group)

        targets = self.targets_for(group)
        results = await self.deployer.gather_each(targets, fetch)
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
