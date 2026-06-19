"""Function workloads: build from Git, then deploy via the shared engine."""

from __future__ import annotations

from app.auth.claims import Principal
from app.core.errors import ServiceUnavailableError
from app.models.common import WorkloadResponse
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
    async def accept(
        self, spec: FunctionCreate, user: Principal, background
    ) -> WorkloadResponse:
        oname = object_name(spec.name, user.primary_group)
        targets = self._engine.deployer.resolve_targets(spec.sites)
        host = self._engine.host_for(spec.name, spec.hostname, user)
        await self._engine.assert_host_available(host, oname, targets)
        await self._engine.assert_workload_absent(spec.name, oname, targets)
        background.add_task(self._engine.run, self.create, spec, user)
        return self._engine.accepted("function", spec.name, host, runtime=spec.runtime)

    async def accept_update(
        self, name: str, spec: FunctionUpdate, user: Principal, background
    ) -> WorkloadResponse:
        await self._engine.load_existing(name, OFFERING_FUNCTION, user)  # 404 if missing
        background.add_task(self._engine.run, self.update, name, spec, user)
        return self._engine.accepted(
            "function", name, self._engine.host_for(name, spec.hostname, user)
        )

    # -- create / update -------------------------------------------------
    async def create(
        self, spec: FunctionCreate, user: Principal
    ) -> tuple[WorkloadResponse, int]:
        group = user.primary_group
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
            image=build.digest or build.image,
            offering=OFFERING_FUNCTION,
            env=spec.env,
            files=spec.files,
            scaling=spec.scaling,
            hostname=spec.hostname,
            sites=spec.sites,
            pull_secret_name=None,
            pull_secret_manifest=None,
            created=True,
        )
        body.type = "function"
        body.runtime = spec.runtime
        body.imageDigest = build.digest
        return body, code

    async def update(
        self, name: str, spec: FunctionUpdate, user: Principal
    ) -> tuple[WorkloadResponse, int]:
        existing = await self._engine.load_existing(name, OFFERING_FUNCTION, user)
        body, code = await self._engine.apply_workload(
            name=name,
            user=user,
            image=existing["image"],  # code changes go through a (re)build flow
            offering=OFFERING_FUNCTION,
            env=spec.env,
            files=spec.files,
            scaling=spec.scaling,
            hostname=spec.hostname,
            sites=None,
            pull_secret_name=None,
            pull_secret_manifest=None,
            created=False,
        )
        body.type = "function"
        return body, code

    # -- read / delete ---------------------------------------------------
    async def get(self, name: str, user: Principal) -> WorkloadResponse:
        return await self._engine.get("function", name, user)

    async def delete(self, name: str, user: Principal) -> None:
        await self._engine.delete("function", name, user)
