"""Function workloads: build from Git, then deploy via the shared engine."""

from __future__ import annotations

from app.auth.claims import Principal
from app.core.errors import ServiceUnavailableError
from app.models.common import WorkloadResponse, WorkloadSummary
from app.models.function import FunctionCreate, FunctionUpdate
from app.services.builder import BuildRequest
from app.services.workloads import OFFERING_FUNCTION, WorkloadService, object_name


class FunctionService:
    """Function-specific orchestration; delegates the shared work to WorkloadService."""

    def __init__(self, engine: WorkloadService):
        self._engine = engine

    # -- async accept (202 + poll) ---------------------------------------
    # Validate synchronously (so ServiceNow gets immediate 400/404/409), then
    # run the build+deploy in the background and return 202 Accepted with a
    # status URL to poll. Deploys (esp. function builds) can be slow.
    async def accept(self, spec: FunctionCreate, user: Principal, background) -> WorkloadResponse:
        group = spec.group
        self._engine.assert_group(user, group)
        oname = object_name(spec.name, group)
        targets = self._engine.deployer.resolve_targets(spec.sites)
        host = self._engine.host_for(spec.name, spec.hostname, group)
        await self._engine.assert_host_available(host, oname, targets)
        await self._engine.assert_workload_absent(spec.name, oname, targets)
        background.add_task(self._engine.run, self.create, spec, user)
        return self._engine.accepted(OFFERING_FUNCTION, spec.name, group, host, runtime=spec.runtime)

    async def accept_update(self, name: str, spec: FunctionUpdate, user: Principal, background) -> WorkloadResponse:
        group = spec.group
        await self._engine.load_existing(name, OFFERING_FUNCTION, user, group)
        background.add_task(self._engine.run, self.update, name, spec, user)
        return self._engine.accepted(
            OFFERING_FUNCTION, name, group, self._engine.host_for(name, spec.hostname, group)
        )

    # -- create / update -------------------------------------------------
    async def create(self, spec: FunctionCreate, user: Principal) -> tuple[WorkloadResponse, int]:
        group = spec.group
        oname = object_name(spec.name, group)
        try:
            build = self._engine.builder.build(
                BuildRequest(
                    name=spec.name,
                    group=group,
                    git_url=spec.gitUrl,
                    branch=spec.branch,
                    git_token=spec.gitToken,
                    runtime=spec.runtime,
                )
            )
        except NotImplementedError as exc:
            raise ServiceUnavailableError(str(exc)) from exc

        await self._engine.assert_workload_absent(
            spec.name, oname, self._engine.deployer.resolve_targets(spec.sites)
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
            created=True,
            runtime=spec.runtime,
            git_url=spec.gitUrl,
            branch=spec.branch,
        )
        body.type = OFFERING_FUNCTION
        body.runtime = spec.runtime
        body.imageDigest = build.digest
        return body, code

    async def update(self, name: str, spec: FunctionUpdate, user: Principal) -> tuple[WorkloadResponse, int]:
        group = spec.group
        existing = await self._engine.load_existing(name, OFFERING_FUNCTION, user, group)
        body, code = await self._engine.apply_workload(
            name=name,
            user=user,
            group=group,
            image=existing["image"],  # code changes go through a (re)build flow
            offering=OFFERING_FUNCTION,
            env=spec.env,
            files=spec.files,
            scaling=spec.scaling,
            size=spec.size,
            hostname=spec.hostname,
            sites=None,
            pull_secret_name=None,
            pull_secret_manifest=None,
            created=False,
            # preserve build metadata across a config-only update
            runtime=existing.get("runtime"),
            git_url=existing.get("gitUrl"),
            branch=existing.get("branch"),
        )
        body.type = OFFERING_FUNCTION
        body.runtime = existing.get("runtime")
        return body, code

    # -- read / delete ---------------------------------------------------
    async def get(self, name: str, group: str, user: Principal) -> WorkloadResponse:
        return await self._engine.get(OFFERING_FUNCTION, name, user, group)

    async def list(self, group: str, user: Principal) -> list[WorkloadSummary]:
        return await self._engine.list_workloads(OFFERING_FUNCTION, user, group)

    async def delete(self, name: str, group: str, user: Principal) -> None:
        await self._engine.delete(OFFERING_FUNCTION, name, user, group)
