"""Function workloads: build from Git, then deploy via the shared engine."""

from __future__ import annotations

from api.auth.claims import Principal
from api.models.common import LogsResponse, WorkloadStatsResponse, WorkloadSummary
from api.models.function import FunctionCreate, FunctionResponse, FunctionUpdate
from api.services.builder.runtimes import RuntimeRegistry
from api.services.offering import FUNCTION
from api.services.state import describe as describe_svc
from api.services.workloads import ApplyRequest, WorkloadService
from common.build import BuildPlan, BuildRequest
from common.errors import ValidationError
from common.labels import OFFERING_FUNCTION, workload_labels
from common.names import object_name, repository_of


class FunctionService:
    """Function-specific orchestration; delegates the shared work to WorkloadService."""

    def __init__(self, engine: WorkloadService, runtimes: RuntimeRegistry):
        """Initialize the service."""
        self._engine = engine
        self._runtimes = runtimes

    def _assert_runtime(self, runtime: str, version: str | None = None) -> None:
        """Reject an unknown/unbuildable runtime or version (400, before the 202).

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

    def _build(self, req: BuildRequest, user: Principal) -> BuildPlan:
        """The owned manifests that declare the build, and the tag they push to."""
        oname = object_name(req.name, req.group)
        labels = workload_labels(req.group, user.username, oname, OFFERING_FUNCTION)
        return self._engine.builder.plan(req, labels)

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
        """Validate and accept a create request, scheduling the build+deploy (202)."""
        self._assert_runtime(spec.runtime, spec.version)
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
        """Validate and accept an update request, scheduling the deploy (202)."""
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

    def _rebuild_request(
        self, name: str, group: str, existing: dict, user: Principal
    ) -> BuildRequest:
        """Reconstruct a deployed function's build inputs, for a rebuild.

        Raises:
            ValidationError: If the stored state cannot describe a build.
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

    async def accept_rebuild(
        self, group: str, name: str, user: Principal, background
    ) -> FunctionResponse:
        """Validate and accept a rebuild request, scheduling the build (202).

        Loaded and authorized synchronously, so a missing workload, a missing
        token or a withdrawn runtime is an immediate 404/400.

        Raises:
            NotFoundError: If no such function exists (or it isn't the caller's).
            ValidationError: If the stored state cannot describe a build.
        """
        existing = await self._engine.load_existing(name, FUNCTION, user, group)
        req = self._rebuild_request(name, group, existing, user)
        self._assert_runtime(req.runtime, req.version)
        background.add_task(self._engine.run, self.rebuild, group, name, user, existing)
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

    async def rebuild(self, group: str, name: str, user: Principal, existing: dict) -> None:
        """Build the function's current source again (runs in the background).

        Raises:
            ValidationError: If the stored state cannot describe a build.
            ServiceUnavailableError: If the build pipeline is unavailable.
        """
        req = self._rebuild_request(name, group, existing, user)
        await self._engine.apply_build(name, group, self._build(req, user))

    async def create(
        self, group: str, spec: FunctionCreate, user: Principal
    ) -> tuple[FunctionResponse, int]:
        """Build from Git and deploy a new function (runs in the background).

        Raises:
            ServiceUnavailableError: If the build pipeline is unavailable.
        """
        plan = self._build(
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
        )

        # No absence probe here: apply_workload runs one combined host+absence pass
        # over the same targets immediately before it mutates, which is both a
        # stronger guard (nothing happens in between) and one fewer cross-site trip.
        body, code = await self._engine.apply_workload(
            ApplyRequest(
                name=spec.name,
                user=user,
                group=group,
                image=plan.tag,
                env=spec.env,
                files=spec.files,
                scaling=spec.scaling,
                size=spec.size,
                hostname=spec.hostname,
                sites=spec.sites,
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
                # The git credential goes to every site so any of them can rebuild
                # after a switchover; only one site gets the Image
                # (docs/BUILDING.md - Active/Active).
                extra_secrets=plan.replicated,
                local_resources=plan.local,
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

        Raises:
            ValidationError: If a rebuild is needed but no token was supplied and
                none is stored.
            ServiceUnavailableError: If a rebuild is requested but the build
                pipeline is unavailable.
        """
        # Reuse the load_existing result from accept_update (already authorized) to
        # avoid a second multi-site fanout; fall back to a fresh fetch otherwise.
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

        # A changed build input (or a rotated token) rebuilds; a config-only edit
        # re-sends the same inputs and must not disturb the running image.
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
        token_rotated = spec.gitToken is not None and spec.gitToken != stored_token
        if build_inputs_changed and token is None:
            raise ValidationError(
                "a git token is required to rebuild; none was supplied and none is stored"
            )

        image = existing["image"]
        replicated: list[dict] = []
        local: list[dict] = []
        if token is not None:
            # Emitted on EVERY update. Re-applying an unchanged spec is a no-op kpack does
            # not rebuild from, but it recreates a missing Image after a switchover.
            plan = self._build(
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
            )
            replicated, local = plan.replicated, plan.local
            # A layout change moves where builds are pushed with nothing about the
            # function changing, so nothing else here would notice
            # (docs/BUILDING.md - Registry layout).
            moved = repository_of(image) != repository_of(plan.tag)
            # Keep the deployed image otherwise: it may be a digest a finished build
            # resolved, and rewriting it back to the tag spawns a pointless revision.
            if build_inputs_changed or token_rotated or moved:
                image = plan.tag

        body, code = await self._engine.apply_workload(
            ApplyRequest(
                name=name,
                user=user,
                group=group,
                image=image,
                env=spec.env,
                files=spec.files,
                scaling=spec.scaling,
                size=spec.size,
                hostname=spec.hostname,
                sites=None,
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
                local_resources=local,
            ),
            FUNCTION,
        )
        return body, code

    async def get(self, name: str, group: str, user: Principal) -> FunctionResponse:
        """Get one function with live per-site status."""
        return await self._engine.get(FUNCTION, name, user, group)

    async def stats(self, name: str, group: str, user: Principal) -> WorkloadStatsResponse:
        """Read the function's live state (the poll view)."""
        return await self._engine.stats(FUNCTION, name, user, group)

    async def logs(
        self,
        name: str,
        group: str,
        user: Principal,
        *,
        container: str,
        since_seconds: int | None,
        limit_bytes: int | None,
    ) -> LogsResponse:
        """Snapshot the function's pod logs from the current site."""
        return await self._engine.logs(
            FUNCTION,
            name,
            user,
            group,
            container=container,
            since_seconds=since_seconds,
            limit_bytes=limit_bytes,
        )

    async def list(self, group: str, user: Principal, sort: str = "name") -> list[WorkloadSummary]:
        """List the group's functions."""
        return await self._engine.list(FUNCTION, user, group, sort)

    async def delete(self, name: str, group: str, user: Principal) -> None:
        """Delete a function and its derived resources."""
        await self._engine.delete(FUNCTION, name, user, group)
