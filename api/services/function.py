"""Function workloads: build from Git, then deploy via the shared engine."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping

from cloudlet_apis.auth import Principal
from pydantic import ValidationError as PydanticValidationError

from api.models.common import (
    PodLogSnapshot,
    PodRoster,
    WorkloadStatsResponse,
    WorkloadSummary,
)
from api.models.function import FunctionCreate, FunctionResponse, FunctionUpdate
from api.services.builder.runtimes import RuntimeRegistry
from api.services.offering import FUNCTION
from api.services.state import describe as describe_svc
from api.services.streams.sse import StreamEvent
from api.services.workloads import ApplyRequest, WorkloadService
from api.services.workloads.service import run_background
from common.build import BuildPlan, BuildRequest
from common.config import RegistryConfig
from common.errors import ValidationError
from common.kpack import MAX_BUILD_WORKLOAD_NAME
from common.labels import OFFERING_FUNCTION, workload_labels
from common.names import object_name, validate_object_name


class FunctionService:
    """Function-specific orchestration; delegates the shared work to WorkloadService."""

    def __init__(self, engine: WorkloadService, runtimes: RuntimeRegistry):
        """Initialize the service.

        Args:
            engine: The shared workload engine doing the cross-region work.
            runtimes: The available-runtimes registry. Required rather than
                defaulted: reaching for the process-wide registry here would
                make the service depend on module state that only the DI layer
                should own, and would put api.dependencies and this module in an
                import cycle.
        """
        self._engine = engine
        self._runtimes = runtimes

    def _assert_runtime(self, runtime: str, version: str | None = None) -> None:
        """Reject an unknown/unbuildable runtime or version (400, before the 202).

        Checking that it maps to a Builder - not just that it exists - is what
        turns a mounted-ConfigMap problem into an immediate, accurate 400. Left
        to the build path it would surface minutes later as a failed background
        deploy, which reads like a broken build rather than broken configuration.

        The version is checked against the same ``versions`` list ``/info``
        advertises, so what a client is offered and what a create accepts cannot
        disagree. Airgapped that is not a formality: only the advertised versions
        are mirrored, so an unlisted one has no toolchain to download and would
        fail deep inside the build.

        Args:
            runtime: The requested runtime.
            version: The requested language version, or None for the default.

        Raises:
            ValidationError: If ``runtime`` isn't available, names no Builder, or
                ``version`` isn't one the runtime offers.
        """
        spec = self._runtimes.get(runtime)
        if spec is None:
            available = ", ".join(self._runtimes.names()) or "none configured"
            raise ValidationError(
                f"unsupported runtime '{runtime}'; available runtimes: {available}"
            )
        if not spec.builder:
            raise ValidationError(
                f"runtime '{runtime}' is not buildable: it maps to no kpack Builder. "
                "The runtimes ConfigMap is missing or incomplete."
            )
        if version is None:
            return
        # No versionEnv means there is no build variable to set, and an empty
        # `versions` means the runtime pins its own - both are "not selectable",
        # so accepting a version would silently ignore it.
        if not spec.versionEnv or not spec.versions:
            raise ValidationError(
                f"runtime '{runtime}' does not offer a choice of version; omit 'version'"
            )
        if version not in spec.versions:
            raise ValidationError(
                f"unsupported version '{version}' for runtime '{runtime}'; "
                f"available versions: {', '.join(spec.versions)}"
            )

    def _plan(
        self, req: BuildRequest, user: Principal, registries: Mapping[str, RegistryConfig]
    ) -> BuildPlan:
        """The owned manifests that declare the build, and the tags they push to.

        Includes the workload's ``{workload}-git`` Secret: one Secret serves both
        the API (reading the token back on a later edit) and kpack (cloning with
        it), because the build runs in the workload's own namespace.

        Args:
            req: The build request.
            user: The authenticated caller, for the ownership labels.
            registries: The registry each targeted region builds into.

        Returns:
            The build plan, one tag and one set of manifests per region.
        """
        oname = object_name(req.name, req.group)
        labels = workload_labels(req.group, user.username, oname, OFFERING_FUNCTION)
        return self._engine.builder.plan(req, labels, registries)

    # Validate synchronously for an immediate 400/404/409, then build and deploy
    # in the background behind a 202 - a function build is slow.
    def _echo(self, spec) -> dict:
        """Submitted config echoed back on the spec (secrets/gitToken never echoed)."""
        return dict(
            size=spec.size,
            scaling=spec.scaling,
            env=describe_svc.redact_env(spec.env),
            files=describe_svc.redact_files(spec.files),
            runtime=spec.runtime,
            version=spec.version,
            gitRepo=spec.gitRepo,
            branch=spec.branch,
            path=spec.path,
            port=spec.port,
        )

    async def accept(
        self, group: str, spec: FunctionCreate, user: Principal, background
    ) -> FunctionResponse:
        """Validate and accept a create request, scheduling the build+deploy (202).

        Args:
            group: The owning group (from the request path).
            spec: The function create request.
            user: The authenticated caller.
            background: FastAPI background tasks to schedule the build+deploy on.

        Returns:
            A Pending response with a ``statusUrl`` to poll.
        """
        self._assert_runtime(spec.runtime, spec.version)
        # A function's pair is capped tighter than the shared check's 63: its
        # build objects prefix it, and kpack writes that prefixed name where a
        # label value has to fit (common/kpack.py - MAX_BUILD_WORKLOAD_NAME).
        try:
            validate_object_name(spec.name, group, limit=MAX_BUILD_WORKLOAD_NAME)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return await self._engine.accept_create(
            offering=FUNCTION,
            group=group,
            spec=spec,
            user=user,
            background=background,
            work=self.create,
            **self._echo(spec),
        )

    async def accept_update(
        self, group: str, name: str, spec: FunctionUpdate, user: Principal, background
    ) -> FunctionResponse:
        """Validate and accept an update request, scheduling the deploy (202).

        Args:
            group: The owning group (from the request path).
            name: The workload name.
            spec: The function update request.
            user: The authenticated caller.
            background: FastAPI background tasks to schedule the deploy on.

        Returns:
            A Pending response with a ``statusUrl`` to poll.
        """
        self._assert_runtime(spec.runtime, spec.version)
        return await self._engine.accept_update(
            offering=FUNCTION,
            group=group,
            name=name,
            spec=spec,
            user=user,
            background=background,
            work=self.update,
            **self._echo(spec),
        )

    def _build_request(
        self, name: str, group: str, existing: dict, user: Principal
    ) -> BuildRequest:
        """Reconstruct the build inputs of a deployed function, for a rebuild.

        Every input comes back off the workload itself - the KSVC's annotations
        and the ``{workload}-git`` Secret - which is the same reconstruction a
        region that has never built the function does after a switchover
        (docs/BUILDING.md - Reconstruction after switchover). Nothing is taken
        from the request: a rebuild asks for the *current* definition to be built
        again, so accepting inputs here would make it an update in disguise.

        Args:
            name: The workload name.
            group: The owning group.
            existing: The state loaded by ``load_existing``.
            user: The authenticated caller, stamped as the build's owner.

        Returns:
            The build request.

        Raises:
            ValidationError: If the stored state cannot describe a build - no
                token, or missing build metadata.
        """
        git_url = existing.get("gitUrl")
        branch = existing.get("branch")
        runtime = existing.get("runtime")
        missing = [
            field
            for field, value in (("gitRepo", git_url), ("branch", branch), ("runtime", runtime))
            if not value
        ]
        if missing:
            raise ValidationError(
                f"cannot rebuild: the function has no stored {', '.join(missing)}; "
                "send the build inputs with a PUT instead"
            )
        token = existing.get("git_token")
        if not token:
            raise ValidationError(
                "cannot rebuild: no git token is stored for this function; "
                "send one with a PUT instead"
            )
        # The only path that builds a BuildRequest out of stored state rather than
        # a validated request body, so it is the only one where the dataclass's own
        # validation can fail. Untranslated that is a 500 for a hand-edited
        # annotation, or for a rule tightened since the function was created.
        try:
            return BuildRequest(
                name=name,
                group=group,
                git_url=git_url,
                branch=branch,
                path=existing.get("path") or "",
                git_token=token,
                runtime=runtime,
                version=existing.get("version"),
                owner=user.username,
            )
        except PydanticValidationError as exc:
            fields = ", ".join(sorted({str(e["loc"][0]) for e in exc.errors() if e.get("loc")}))
            raise ValidationError(
                f"cannot rebuild: the stored build inputs are not valid ({fields}); "
                "send them with a PUT instead"
            ) from exc

    async def accept_build(
        self, group: str, name: str, user: Principal, background
    ) -> FunctionResponse:
        """Validate and accept a rebuild request, scheduling the build (202).

        Loads and authorizes the function synchronously, so a missing workload,
        a missing token or a runtime that has since left the ConfigMap is an
        immediate 404/400 rather than a background failure nobody sees.

        Args:
            group: The owning group (from the request path).
            name: The workload name.
            user: The authenticated caller.
            background: FastAPI background tasks to schedule the build on.

        Returns:
            A Pending response with a ``statusUrl`` to poll.

        Raises:
            NotFoundError: If no such function exists (or it isn't the caller's).
            ValidationError: If the stored state cannot describe a build.
        """
        existing = await self._engine.load_existing(name, FUNCTION, user, group)
        req = self._build_request(name, group, existing, user)
        # A runtime can be removed from the ConfigMap after a function was built
        # with it; that is a 400 now, not a build that fails minutes later.
        self._assert_runtime(req.runtime, req.version)
        background.add_task(run_background, self.build, group, name, user, existing)
        host = existing.get("host") or self._engine.host_for(name, None, group)
        return self._engine.accepted(
            FUNCTION,
            name,
            group,
            host,
            runtime=req.runtime,
            version=req.version,
            gitRepo=req.git_url,
            branch=req.branch,
            path=req.path,
        )

    async def build(self, group: str, name: str, user: Principal, existing: dict) -> None:
        """Build a function's current source again (runs in the background).

        The image is rebuilt from the same repository, branch and runtime it
        already has, so this is what picks up a base-image or dependency change,
        retries a failed build, or gets a pushed commit built now rather than
        when kpack next polls. Nothing about the workload changes, so the KSVC is
        not written and the running revision keeps serving until the new digest
        is rolled out (docs/BUILDING.md - Ownership: API vs Build Service).

        Args:
            group: The owning group (from the request path).
            name: The workload name.
            user: The authenticated caller.
            existing: The workload state preloaded (and authorized) by
                :meth:`accept_build`.

        Raises:
            ValidationError: If the stored state cannot describe a build.
            ServiceUnavailableError: If the build pipeline is unavailable.
        """
        req = self._build_request(name, group, existing, user)
        # Every configured region; apply_build skips the ones not running it.
        registries = self._engine.target_registries(None)
        await self._engine.apply_build(name, group, self._plan(req, user, registries))

    async def create(
        self, group: str, spec: FunctionCreate, user: Principal
    ) -> tuple[FunctionResponse, int]:
        """Build from Git and deploy a new function (runs in the background).

        Args:
            group: The owning group (from the request path).
            spec: The function create request.
            user: The authenticated caller.

        Returns:
            The response body and HTTP status code.

        Raises:
            ServiceUnavailableError: If the build pipeline is unavailable.
        """
        # A region builds what it runs, so the plan covers exactly the targets.
        registries = self._engine.target_registries(spec.regions)
        plan = self._plan(
            BuildRequest(
                name=spec.name,
                group=group,
                git_url=spec.gitRepo,
                branch=spec.branch,
                path=spec.path,
                git_token=spec.gitToken,
                runtime=spec.runtime,
                version=spec.version,
                owner=user.username,
            ),
            user,
            registries,
        )

        # No absence probe here: apply_workload runs one combined host+absence pass
        # over the same targets immediately before it mutates, which is both a
        # stronger guard (nothing happens in between) and one fewer cross-region trip.
        body, code = await self._engine.apply_workload(
            ApplyRequest(
                name=spec.name,
                user=user,
                group=group,
                # No single image: each region deploys at the tag its own build
                # pushes to, and reads Building until something lands there.
                image="",
                images=plan.tags,
                env=spec.env,
                files=spec.files,
                scaling=spec.scaling,
                size=spec.size,
                hostname=spec.hostname,
                regions=spec.regions,
                port=spec.port,
                # Pulled with the same credential kpack pushed with. The Secret is the
                # chart's, shared by every function, so it is referenced, never applied.
                pull_secret_name=self._engine.builder.pull_secret,
                created=True,
                runtime=spec.runtime,
                version=spec.version,
                git_url=spec.gitRepo,
                branch=spec.branch,
                path=spec.path,
                # The git credential goes to every region so any of them can
                # rebuild; each region gets its own Image (docs/BUILDING.md -
                # Active/Active Behaviour).
                extra_secrets=plan.replicated,
                region_resources=plan.manifests_by_region,
            ),
            FUNCTION,
        )
        return body, code

    async def update(
        self,
        group: str,
        name: str,
        spec: FunctionUpdate,
        user: Principal,
        existing: dict | None = None,
    ) -> tuple[FunctionResponse, int]:
        """Apply an update to a function, rebuilding on a build-input or token change.

        Args:
            group: The owning group (from the request path).
            name: The workload name.
            spec: The function update request.
            user: The authenticated caller.
            existing: The workload state preloaded by accept_update, if any; a
                fresh fetch is done when None.

        Returns:
            The response body and HTTP status code.

        Raises:
            ValidationError: If a rebuild is needed but no token was supplied and
                none is stored.
            ServiceUnavailableError: If a rebuild is requested but the build
                pipeline is unavailable.
        """
        # Reuse the load_existing result from accept_update (already authorized) to
        # avoid a second multi-region fanout; fall back to a fresh fetch otherwise.
        if existing is None:
            existing = await self._engine.load_existing(name, FUNCTION, user, group)

        # Full replace, so the build inputs are the request's. The token is the
        # redacted keep: the stored one is reused unless the client sent a new one.
        runtime = spec.runtime
        version = spec.version
        git_url = spec.gitRepo
        branch = spec.branch
        path = spec.path
        stored_token = existing.get("git_token")
        token = spec.gitToken or stored_token

        # Only to refuse an untokenised rebuild; what rebuilds is kpack's own
        # diff of the Image spec, not this.
        build_inputs_changed = (
            git_url != existing.get("gitUrl")
            or branch != existing.get("branch")
            or path != (existing.get("path") or "")
            or runtime != existing.get("runtime")
            # A version change is a build input like any other. Omitting it returns
            # the function to the platform default, which is also a rebuild - the
            # field is replaced, not kept, like branch and runtime.
            or version != existing.get("version")
        )
        if build_inputs_changed and token is None:
            raise ValidationError(
                "a git token is required to rebuild; none was supplied and none is stored"
            )

        # Never rewritten here, whatever changed. After the create, the image is
        # the controller's alone (docs/BUILDING.md - Digest propagation): the
        # build this update declares has not pushed yet, so anything written now
        # is a revision of the code already running. Per region, because each runs
        # what its own build pushed - one value would move a peer onto this
        # region's registry.
        image = existing["image"]
        images = dict(existing.get("images") or {})
        registries = self._engine.target_registries(None)
        replicated: list[dict] = []
        region_resources: dict[str, list[dict]] = {}
        if token is not None:
            # Emitted on EVERY update. Re-applying an unchanged spec is a no-op kpack does
            # not rebuild from, but it recreates a missing Image after a switchover.
            plan = self._plan(
                BuildRequest(
                    name=name,
                    group=group,
                    git_url=git_url,
                    branch=branch,
                    path=path,
                    git_token=token,
                    runtime=runtime,
                    version=version,
                    owner=user.username,
                ),
                user,
                registries,
            )
            replicated = plan.replicated
            region_resources = plan.manifests_by_region
            # A region not running it yet deploys at its own tag and reads
            # Building until its first build lands, exactly as a create does.
            for region, tag in plan.tags.items():
                images.setdefault(region, tag)

        body, code = await self._engine.apply_workload(
            ApplyRequest(
                name=name,
                user=user,
                group=group,
                image=image,
                images=images,
                env=spec.env,
                files=spec.files,
                scaling=spec.scaling,
                size=spec.size,
                hostname=spec.hostname,
                regions=None,
                # Replaced like every other non-secret field: omitting it returns
                # the function to 8080, as omitting `version` returns it to the
                # platform's default runtime version.
                port=spec.port,
                pull_secret_name=self._engine.builder.pull_secret,
                created=False,
                # stamp the (possibly updated) build metadata; never the token
                runtime=runtime,
                version=version,
                git_url=git_url,
                branch=branch,
                path=path,
                prev_host=existing.get("host"),
                kept_env=existing.get("env_values"),
                kept_files=existing.get("files_values"),
                extra_secrets=replicated,
                region_resources=region_resources,
            ),
            FUNCTION,
        )
        return body, code

    async def get(self, name: str, group: str, user: Principal) -> FunctionResponse:
        """Get one function with live per-region status.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.

        Returns:
            The full single-function response.
        """
        return await self._engine.get(FUNCTION, name, user, group)

    async def stats(self, name: str, group: str, user: Principal) -> WorkloadStatsResponse:
        """Read the function's live state (the poll view).

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.

        Returns:
            The rollup plus per-region replicas and usage.
        """
        return await self._engine.stats(FUNCTION, name, user, group)

    async def pods(self, name: str, group: str, user: Principal) -> PodRoster:
        """The function's pods on the current region, read once.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.

        Returns:
            The roster.
        """
        return await self._engine.pods(FUNCTION, name, user, group)

    async def pod_logs(
        self,
        name: str,
        group: str,
        user: Principal,
        *,
        pod: str,
        container: str,
        since_seconds: int | None,
        limit_bytes: int | None,
        tail_lines: int | None = None,
    ) -> PodLogSnapshot:
        """One of the function's pods' logs as it stands, read once.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.
            pod: The pod to read.
            container: The pod container to read.
            since_seconds: Only lines newer than this, if set.
            limit_bytes: Cap on the bytes read, if set.
            tail_lines: Newest lines wanted, if set; clamped to the snapshot bound.

        Returns:
            The snapshot.
        """
        return await self._engine.pod_logs(
            FUNCTION,
            name,
            user,
            group,
            pod=pod,
            container=container,
            since_seconds=since_seconds,
            limit_bytes=limit_bytes,
            tail_lines=tail_lines,
        )

    async def stream_pods(
        self, name: str, group: str, user: Principal, *, interval: float | None
    ) -> AsyncIterator[StreamEvent]:
        """Stream which pods the function has on the current region.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.
            interval: Seconds between listings; None takes the default.

        Returns:
            The event stream.
        """
        return await self._engine.stream_pods(FUNCTION, name, user, group, interval=interval)

    async def stream_pod_logs(
        self,
        name: str,
        group: str,
        user: Principal,
        *,
        pod: str,
        container: str,
        since_seconds: int | None,
        tail_lines: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Follow one of the function's pods' logs, on the current region.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.
            pod: The pod to follow.
            container: The pod container to read.
            since_seconds: How far back the log starts, if set.
            tail_lines: Start at the newest this-many lines instead, if set.

        Returns:
            The event stream.
        """
        return await self._engine.stream_pod_logs(
            FUNCTION,
            name,
            user,
            group,
            pod=pod,
            container=container,
            since_seconds=since_seconds,
            tail_lines=tail_lines,
        )

    async def stream_stats(
        self, name: str, group: str, user: Principal, *, interval: float | None
    ) -> AsyncIterator[StreamEvent]:
        """Push the function's live state on an interval.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.
            interval: Seconds between readings; None takes the default.

        Returns:
            The event stream.
        """
        return await self._engine.stream_stats(FUNCTION, name, user, group, interval=interval)

    async def list(self, group: str, user: Principal, sort: str = "name") -> list[WorkloadSummary]:
        """List the group's functions.

        Args:
            group: The owning group.
            user: The authenticated caller.
            sort: Sort key, "name" or "createdAt".

        Returns:
            The per-workload summaries.
        """
        return await self._engine.list(FUNCTION, user, group, sort)

    async def delete(self, name: str, group: str, user: Principal) -> None:
        """Delete a function and its derived resources.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.
        """
        await self._engine.delete(FUNCTION, name, user, group)
