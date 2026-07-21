"""Function workloads: build from Git, then deploy via the shared engine."""

from __future__ import annotations

from api.auth.claims import Principal
from api.models.common import LogsResponse, WorkloadSummary
from api.models.function import FunctionCreate, FunctionResponse, FunctionUpdate
from api.services import describe as describe_svc
from api.services import secrets as secret_svc
from api.services.runtimes import RuntimeRegistry, get_runtimes
from api.services.workloads import OFFERING_FUNCTION, WorkloadService, object_name
from common.contract import BuildRequest
from common.errors import ServiceUnavailableError, ValidationError
from common.labels import workload_labels


class FunctionService:
    """Function-specific orchestration; delegates the shared work to WorkloadService."""

    def __init__(self, engine: WorkloadService, runtimes: RuntimeRegistry | None = None):
        """Initialize the service.

        Args:
            engine: The shared workload engine doing the cross-site work.
            runtimes: The available-runtimes registry; defaults to the process
                registry loaded from the mounted config file.
        """
        self._engine = engine
        self._runtimes = runtimes or get_runtimes()

    def _assert_runtime(self, runtime: str) -> None:
        """Reject a runtime not in the registry (synchronous 400, before accept).

        Args:
            runtime: The requested runtime.

        Raises:
            ValidationError: If ``runtime`` isn't an available runtime.
        """
        if not self._runtimes.has(runtime):
            available = ", ".join(self._runtimes.names())
            raise ValidationError(
                f"unsupported runtime '{runtime}'; available runtimes: {available}"
            )

    def _git_secret(self, name: str, group: str, user: Principal, token: str) -> dict:
        """Build the ``{workload}-git`` Secret holding the git token."""
        oname = object_name(name, group)
        return secret_svc.build_git_secret(
            secret_svc.git_secret_name(oname),
            workload_labels(group, user.username, oname, OFFERING_FUNCTION),
            token,
        )

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
        if spec.runtime is not None:
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
        try:
            build = self._engine.builder.build(
                BuildRequest(
                    name=spec.name,
                    group=group,
                    git_url=spec.gitRepo,
                    branch=spec.branch,
                    git_token=spec.gitToken,
                    runtime=spec.runtime,
                )
            )
        except NotImplementedError as exc:
            raise ServiceUnavailableError(str(exc)) from exc

        await self._engine.assert_workload_absent(
            spec.name, group, self._engine.deployer.resolve_targets(spec.sites)
        )
        body, code = await self._engine.apply_workload(
            name=spec.name,
            user=user,
            group=group,
            image=build.digest or build.image,
            offering=OFFERING_FUNCTION,
            env=spec.env,
            files=spec.files,
            scaling=spec.scaling,
            size=spec.size,
            hostname=spec.hostname,
            sites=spec.sites,
            pull_secret_name=None,
            pull_secret_manifest=None,
            port=None,
            created=True,
            runtime=spec.runtime,
            git_url=spec.gitRepo,
            branch=spec.branch,
            # Persist the git token so a later edit can rebuild without re-sending it.
            extra_secrets=[self._git_secret(spec.name, group, user, spec.gitToken)],
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

        # Build inputs default to the existing ones. The git token is stored, so the
        # effective token is the stored one unless the client sent a new one.
        runtime = spec.runtime or existing.get("runtime")
        git_url = spec.gitRepo or existing.get("gitUrl")
        branch = spec.branch or existing.get("branch") or "main"
        stored_token = existing.get("git_token")
        token = spec.gitToken or stored_token

        # Rebuild when a build input actually changes, or when the token is rotated
        # (a client echoing an unchanged spec back on a config-only edit does not
        # rebuild). Otherwise keep the current image.
        build_inputs_changed = (
            (spec.gitRepo is not None and spec.gitRepo != existing.get("gitUrl"))
            or (spec.branch is not None and spec.branch != existing.get("branch"))
            or (spec.runtime is not None and spec.runtime != existing.get("runtime"))
        )
        token_rotated = spec.gitToken is not None and spec.gitToken != stored_token
        if build_inputs_changed or token_rotated:
            if token is None:
                raise ValidationError(
                    "a git token is required to rebuild; none was supplied and none is stored"
                )
            try:
                build = self._engine.builder.build(
                    BuildRequest(
                        name=name,
                        group=group,
                        git_url=git_url,
                        branch=branch,
                        git_token=token,
                        runtime=runtime,
                    )
                )
            except NotImplementedError as exc:
                raise ServiceUnavailableError(str(exc)) from exc
            image = build.digest or build.image
        else:
            image = existing["image"]

        # Re-store the token only when the client supplied one (rotation); omitting
        # it leaves the stored copy in place (extra_secrets empty -> not pruned).
        extra_secrets = (
            [self._git_secret(name, group, user, spec.gitToken)]
            if spec.gitToken is not None
            else []
        )
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
            pull_secret_name=None,
            pull_secret_manifest=None,
            port=None,
            created=False,
            # stamp the (possibly updated) build metadata; never the token
            runtime=runtime,
            git_url=git_url,
            branch=branch,
            prev_host=existing.get("host"),
            kept_env=existing.get("env_values"),
            kept_files=existing.get("files_values"),
            extra_secrets=extra_secrets,
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
