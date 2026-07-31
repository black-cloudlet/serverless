"""Function workloads: build from Git, then deploy via the shared engine."""

from __future__ import annotations

from api.auth.claims import Principal
from api.models.common import LogsResponse, WorkloadSummary
from api.models.function import FunctionCreate, FunctionResponse, FunctionUpdate
from api.services import describe as describe_svc
from api.services.runtimes import RuntimeRegistry
from api.services.workloads import OFFERING_FUNCTION, WorkloadService, object_name
from common.contract import BuildPlan, BuildRequest
from common.errors import ValidationError
from common.labels import workload_labels


class FunctionService:
    """Function-specific orchestration; delegates the shared work to WorkloadService."""

    def __init__(self, engine: WorkloadService, runtimes: RuntimeRegistry):
        """Initialize the service.

        Args:
            engine: The shared workload engine doing the cross-site work.
            runtimes: The available-runtimes registry. Required rather than
                defaulted: reaching for the process-wide registry here would
                make the service depend on module state that only the DI layer
                should own, and would put api.dependencies and this module in an
                import cycle.
        """
        self._engine = engine
        self._runtimes = runtimes

    def _assert_runtime(self, runtime: str) -> None:
        """Reject a runtime that is unknown or unbuildable (400, before the 202).

        Checking that it maps to a Builder - not just that it exists - is what
        turns a mounted-ConfigMap problem into an immediate, accurate 400. Left
        to the build path it would surface minutes later as a failed background
        deploy, which reads like a broken build rather than broken configuration.

        Args:
            runtime: The requested runtime.

        Raises:
            ValidationError: If ``runtime`` isn't available, or names no Builder.
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

    def _build(self, req: BuildRequest, user: Principal) -> BuildPlan:
        """The owned manifests that declare the build, and the tag they push to.

        Includes the workload's ``{workload}-git`` Secret: one Secret serves both
        the API (reading the token back on a later edit) and kpack (cloning with
        it), because the build runs in the workload's own namespace.

        Args:
            req: The build request.
            user: The authenticated caller, for the ownership labels.

        Returns:
            The build plan.
        """
        oname = object_name(req.name, req.group)
        labels = workload_labels(req.group, user.username, oname, OFFERING_FUNCTION)
        return self._engine.builder.plan(req, labels)

    # Validate synchronously (so ServiceNow gets immediate 400/404/409), then
    # run the build+deploy in the background and return 202 Accepted with a
    # status URL to poll. Deploys (esp. function builds) can be slow.
    def _echo(self, spec) -> dict:
        """Submitted config echoed back on the spec (secrets/gitToken never echoed)."""
        return dict(
            size=spec.size,
            scaling=spec.scaling,
            env=describe_svc.redact_env(spec.env),
            files=describe_svc.redact_files(spec.files),
            runtime=spec.runtime,
            gitRepo=spec.gitRepo,
            branch=spec.branch,
            path=spec.path,
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
        self._assert_runtime(spec.runtime)
        return await self._engine.accept_create(
            offering=OFFERING_FUNCTION,
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
        self._assert_runtime(spec.runtime)
        return await self._engine.accept_update(
            offering=OFFERING_FUNCTION,
            group=group,
            name=name,
            spec=spec,
            user=user,
            background=background,
            work=self.update,
            **self._echo(spec),
        )

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
        plan = self._build(
            BuildRequest(
                name=spec.name,
                group=group,
                git_url=spec.gitRepo,
                branch=spec.branch,
                path=spec.path,
                git_token=spec.gitToken,
                runtime=spec.runtime,
                owner=user.username,
            ),
            user,
        )

        await self._engine.assert_workload_absent(
            spec.name, group, self._engine.deployer.resolve_targets(spec.sites)
        )
        body, code = await self._engine.apply_workload(
            name=spec.name,
            user=user,
            group=group,
            image=plan.tag,
            offering=OFFERING_FUNCTION,
            env=spec.env,
            files=spec.files,
            scaling=spec.scaling,
            size=spec.size,
            hostname=spec.hostname,
            sites=spec.sites,
            # The image is on the platform's own registry, so it pulls with the
            # same credential kpack pushed it with. The Secret is the chart's,
            # shared by every function, so it is referenced and never applied or
            # pruned here.
            pull_secret_name=self._engine.builder.pull_secret,
            pull_secret_manifest=None,
            port=None,
            created=True,
            runtime=spec.runtime,
            git_url=spec.gitRepo,
            branch=spec.branch,
            path=spec.path,
            # The git credential goes to every site so any of them can rebuild
            # after a switchover; only one site gets the Image (§9.5).
            extra_secrets=plan.replicated,
            local_resources=plan.local,
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
        # avoid a second multi-site fanout; fall back to a fresh fetch otherwise.
        if existing is None:
            existing = await self._engine.load_existing(name, OFFERING_FUNCTION, user, group)

        # Full replace: the build inputs are the request's (gitRepo/runtime required,
        # branch defaults to "main"). The git token is the redacted keep - the stored
        # one is reused unless the client sent a new one.
        runtime = spec.runtime
        git_url = spec.gitRepo
        branch = spec.branch
        path = spec.path
        stored_token = existing.get("git_token")
        token = spec.gitToken or stored_token

        # A build input change (or a rotated token) means the image must be
        # rebuilt; a config-only edit re-sends the same inputs and must not
        # disturb the running image.
        build_inputs_changed = (
            git_url != existing.get("gitUrl")
            or branch != existing.get("branch")
            or path != (existing.get("path") or "")
            or runtime != existing.get("runtime")
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
            # Emitted on EVERY update, not only when an input changed. The
            # manifests are a pure function of the request, so re-applying an
            # unchanged spec is a no-op that kpack does not rebuild from - but it
            # recreates the Image on a site that has never had one, which is what
            # makes an update after a switchover self-healing (§9.5).
            plan = self._build(
                BuildRequest(
                    name=name,
                    group=group,
                    git_url=git_url,
                    branch=branch,
                    path=path,
                    git_token=token,
                    runtime=runtime,
                    owner=user.username,
                ),
                user,
            )
            replicated, local = plan.replicated, plan.local
            # Only move the KSVC when the build inputs actually changed. Otherwise
            # keep what is deployed: it may be a digest the build service resolved
            # from a completed build, and rewriting it back to the tag would spawn
            # a pointless revision.
            if build_inputs_changed or token_rotated:
                image = plan.tag

        body, code = await self._engine.apply_workload(
            name=name,
            user=user,
            group=group,
            image=image,
            offering=OFFERING_FUNCTION,
            env=spec.env,
            files=spec.files,
            scaling=spec.scaling,
            size=spec.size,
            hostname=spec.hostname,
            sites=None,
            pull_secret_name=self._engine.builder.pull_secret,
            pull_secret_manifest=None,
            port=None,
            created=False,
            # stamp the (possibly updated) build metadata; never the token
            runtime=runtime,
            git_url=git_url,
            branch=branch,
            path=path,
            prev_host=existing.get("host"),
            kept_env=existing.get("env_values"),
            kept_files=existing.get("files_values"),
            extra_secrets=replicated,
            local_resources=local,
        )
        return body, code

    async def get(self, name: str, group: str, user: Principal) -> FunctionResponse:
        """Get one function with live per-site status.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.

        Returns:
            The full single-function response.
        """
        return await self._engine.get(OFFERING_FUNCTION, name, user, group)

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
        """Snapshot the function's pod logs from the current site.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.
            container: The pod container to read.
            since_seconds: Only logs newer than this, if set.
            limit_bytes: Cap on bytes read per pod, if set.

        Returns:
            The function's per-pod logs from the local site.
        """
        return await self._engine.logs(
            OFFERING_FUNCTION,
            name,
            user,
            group,
            container=container,
            since_seconds=since_seconds,
            limit_bytes=limit_bytes,
        )

    async def list(self, group: str, user: Principal, sort: str = "name") -> list[WorkloadSummary]:
        """List the group's functions.

        Args:
            group: The owning group.
            user: The authenticated caller.
            sort: Sort key, "name" or "createdAt".

        Returns:
            The per-workload summaries.
        """
        return await self._engine.list(OFFERING_FUNCTION, user, group, sort)

    async def delete(self, name: str, group: str, user: Principal) -> None:
        """Delete a function and its derived resources.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.
        """
        await self._engine.delete(OFFERING_FUNCTION, name, user, group)
