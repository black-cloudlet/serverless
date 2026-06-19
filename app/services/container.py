"""Container workloads: deploy a supplied image (+ pull secret) via the shared engine."""

from __future__ import annotations

from app.auth.claims import Principal
from app.core.config import RegistryConfig
from app.models.common import WorkloadResponse
from app.models.container import ContainerCreate, ContainerUpdate
from app.services import secrets as secret_svc
from app.services.labels import workload_labels
from app.services.workloads import OFFERING_CONTAINER, WorkloadService, object_name


class ContainerService:
    """Container-specific orchestration; delegates the shared work to WorkloadService."""

    def __init__(self, engine: WorkloadService, registry: RegistryConfig):
        self._engine = engine
        self._registry = registry

    # -- async accept (202 + poll) ---------------------------------------
    async def accept(self, spec: ContainerCreate, user: Principal, background) -> WorkloadResponse:
        oname = object_name(spec.name, user.primary_group)
        
        targets = self._engine.deployer.resolve_targets(spec.sites)
        host = self._engine.host_for(spec.name, spec.hostname, user)
        
        await self._engine.assert_host_available(host, oname, targets)
        await self._engine.assert_workload_absent(spec.name, oname, targets)
        
        background.add_task(self._engine.run, self.create, spec, user)
        return self._engine.accepted(OFFERING_CONTAINER, spec.name, host, image=spec.image)

    async def accept_update(self, name: str, spec: ContainerUpdate, user: Principal, background) -> WorkloadResponse:
        await self._engine.load_existing(name, OFFERING_CONTAINER, user)
        
        background.add_task(self._engine.run, self.update, name, spec, user)
        
        return self._engine.accepted(
            OFFERING_CONTAINER,
            name, 
            self._engine.host_for(name, spec.hostname, user),
            image=spec.image,
        )

    # -- create / update -------------------------------------------------
    async def create(self, spec: ContainerCreate, user: Principal) -> tuple[WorkloadResponse, int]:
        group = user.primary_group
        oname = object_name(spec.name, group)
        pull_name = f"{oname}-pull"
        
        pull = secret_svc.build_pull_secret(
            pull_name,
            workload_labels(group, user.username, oname, OFFERING_CONTAINER),
            self._registry.url,
            spec.registryUsername,
            spec.registryToken,
        )
        
        await self._engine.assert_workload_absent(
            spec.name, oname, self._engine.deployer.resolve_targets(spec.sites)
        )
        body, code = await self._engine.apply_workload(
            name=spec.name,
            user=user,
            image=spec.image,
            offering=OFFERING_CONTAINER,
            env=spec.env,
            files=spec.files,
            scaling=spec.scaling,
            hostname=spec.hostname,
            sites=spec.sites,
            pull_secret_name=pull_name,
            pull_secret_manifest=pull,
            created=True,
        )
        body.type = OFFERING_CONTAINER
        body.image = spec.image
        return body, code

    async def update(self, name: str, spec: ContainerUpdate, user: Principal) -> tuple[WorkloadResponse, int]:
        existing = await self._engine.load_existing(name, OFFERING_CONTAINER, user)
        oname = object_name(name, user.primary_group)
        image = spec.image or existing["image"]
        body, code = await self._engine.apply_workload(
            name=name,
            user=user,
            image=image,
            offering=OFFERING_CONTAINER,
            env=spec.env,
            files=spec.files,
            scaling=spec.scaling,
            hostname=spec.hostname,
            sites=None,
            pull_secret_name=f"{oname}-pull",
            pull_secret_manifest=None,
            created=False,
        )
        body.type = OFFERING_CONTAINER
        body.image = image
        return body, code

    # -- read / delete ---------------------------------------------------
    async def get(self, name: str, user: Principal) -> WorkloadResponse:
        return await self._engine.get(OFFERING_CONTAINER, name, user)

    async def delete(self, name: str, user: Principal) -> None:
        await self._engine.delete(OFFERING_CONTAINER, name, user)
