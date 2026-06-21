"""Container workloads: deploy a supplied image (+ pull secret) via the shared engine."""

from __future__ import annotations

from app.auth.claims import Principal
from app.models.common import WorkloadSummary
from app.models.container import ContainerCreate, ContainerResponse, ContainerUpdate
from app.services import describe as describe_svc
from app.services import secrets as secret_svc
from app.services.labels import workload_labels
from app.services.workloads import OFFERING_CONTAINER, WorkloadService, object_name


class ContainerService:
    """Container-specific orchestration; delegates the shared work to WorkloadService."""

    def __init__(self, engine: WorkloadService):
        self._engine = engine

    # -- async accept (202 + poll) ---------------------------------------
    def _echo(self, spec) -> dict:
        """Submitted config echoed back on the spec (secrets redacted)."""
        return dict(
            size=spec.size,
            scaling=spec.scaling,
            env=describe_svc.redact_env(spec.env),
            files=describe_svc.redact_files(spec.files),
            registryUsername=spec.registryUsername,
        )

    async def accept(self, spec: ContainerCreate, user: Principal, background) -> ContainerResponse:
        group = spec.group
        self._engine.assert_group(user, group)
        oname = object_name(spec.name, group)

        targets = self._engine.deployer.resolve_targets(spec.sites)
        host = self._engine.host_for(spec.name, spec.hostname, group)

        await self._engine.assert_host_available(host, oname, targets)
        await self._engine.assert_workload_absent(spec.name, oname, targets)

        background.add_task(self._engine.run, self.create, spec, user)
        return self._engine.accepted(
            OFFERING_CONTAINER, spec.name, group, host, image=spec.image, **self._echo(spec)
        )

    async def accept_update(self, name: str, spec: ContainerUpdate, user: Principal, background) -> ContainerResponse:
        group = spec.group
        await self._engine.load_existing(name, OFFERING_CONTAINER, user, group)

        background.add_task(self._engine.run, self.update, name, spec, user)

        return self._engine.accepted(
            OFFERING_CONTAINER,
            name,
            group,
            self._engine.host_for(name, spec.hostname, group),
            image=spec.image,
            **self._echo(spec),
        )

    # -- create / update -------------------------------------------------
    async def create(self, spec: ContainerCreate, user: Principal) -> tuple[ContainerResponse, int]:
        group = spec.group
        oname = object_name(spec.name, group)
        # Registry creds are optional (public image -> no pull secret).
        pull_name: str | None = None
        pull: dict | None = None
        if spec.registryUsername and spec.registryToken:
            pull_name = f"{oname}-pull"
            pull = secret_svc.build_pull_secret(
                pull_name,
                workload_labels(group, user.username, oname, OFFERING_CONTAINER),
                secret_svc.registry_of(spec.image),  # key to the image's registry
                spec.registryUsername,
                spec.registryToken,
            )

        await self._engine.assert_workload_absent(
            spec.name, oname, self._engine.deployer.resolve_targets(spec.sites)
        )
        body, code = await self._engine.apply_workload(
            name=spec.name,
            user=user,
            group=group,
            image=spec.image,
            offering=OFFERING_CONTAINER,
            env=spec.env,
            files=spec.files,
            scaling=spec.scaling,
            size=spec.size,
            hostname=spec.hostname,
            sites=spec.sites,
            pull_secret_name=pull_name,
            pull_secret_manifest=pull,
            created=True,
        )
        body.registryUsername = spec.registryUsername
        return body, code

    async def update(self, name: str, spec: ContainerUpdate, user: Principal) -> tuple[ContainerResponse, int]:
        group = spec.group
        oname = object_name(name, group)
        existing = await self._engine.load_existing(name, OFFERING_CONTAINER, user, group)
        image = spec.image or existing["image"]

        # New creds -> (re)build the pull secret; otherwise carry the existing one
        # forward (None if the image is public).
        pull_name = existing.get("pull_secret")
        pull: dict | None = None
        if spec.registryUsername and spec.registryToken:
            pull_name = f"{oname}-pull"
            pull = secret_svc.build_pull_secret(
                pull_name,
                workload_labels(group, user.username, oname, OFFERING_CONTAINER),
                secret_svc.registry_of(image),  # key to the (effective) image's registry
                spec.registryUsername,
                spec.registryToken,
            )

        body, code = await self._engine.apply_workload(
            name=name,
            user=user,
            group=group,
            image=image,
            offering=OFFERING_CONTAINER,
            env=spec.env,
            files=spec.files,
            scaling=spec.scaling,
            size=spec.size,
            hostname=spec.hostname,
            sites=None,
            pull_secret_name=pull_name,
            pull_secret_manifest=pull,
            created=False,
        )
        body.registryUsername = spec.registryUsername
        return body, code

    # -- read / delete ---------------------------------------------------
    async def get(self, name: str, group: str, user: Principal) -> ContainerResponse:
        return await self._engine.get(OFFERING_CONTAINER, name, user, group)

    async def list(self, group: str, user: Principal, sort: str = "name") -> list[WorkloadSummary]:
        return await self._engine.list_workloads(OFFERING_CONTAINER, user, group, sort)

    async def delete(self, name: str, group: str, user: Principal) -> None:
        await self._engine.delete(OFFERING_CONTAINER, name, user, group)
