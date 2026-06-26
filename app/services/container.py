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
        """Initialize the service.

        Args:
            engine: The shared workload engine doing the cross-site work.
        """
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
        """Validate and accept a create request, scheduling the deploy (202).

        Args:
            spec: The container create request.
            user: The authenticated caller.
            background: FastAPI background tasks to schedule the deploy on.

        Returns:
            A Pending response with a ``statusUrl`` to poll.
        """
        group = spec.group
        self._engine.assert_group(user, group)
        oname = object_name(spec.name, group)

        targets = self._engine.deployer.resolve_targets(spec.sites)
        host = self._engine.host_for(spec.name, spec.hostname, group)
        # Surface deploy-time spec validation synchronously (400), before the 202.
        self._engine.validate_spec(spec.name, group, user.username, spec.env, spec.files)

        await self._engine.assert_host_available(host, oname, targets)
        await self._engine.assert_workload_absent(spec.name, oname, targets)

        background.add_task(self._engine.run, self.create, spec, user)
        return self._engine.accepted(
            OFFERING_CONTAINER, spec.name, group, host, image=spec.image, **self._echo(spec)
        )

    async def accept_update(
        self, name: str, spec: ContainerUpdate, user: Principal, background
    ) -> ContainerResponse:
        """Validate and accept an update request, scheduling the deploy (202).

        Args:
            name: The workload name.
            spec: The container update request.
            user: The authenticated caller.
            background: FastAPI background tasks to schedule the deploy on.

        Returns:
            A Pending response with a ``statusUrl`` to poll.
        """
        group = spec.group
        existing = await self._engine.load_existing(name, OFFERING_CONTAINER, user, group)
        # Surface deploy-time spec validation synchronously (400), before the 202.
        self._engine.validate_spec(name, group, user.username, spec.env, spec.files)

        background.add_task(self._engine.run, self.update, name, spec, user, existing)

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
        """Deploy a new container (runs in the background after accept).

        Builds the pull secret (if registry creds were given) and applies the
        workload to all target sites.

        Args:
            spec: The container create request.
            user: The authenticated caller.

        Returns:
            The response body and HTTP status code.
        """
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

    async def update(
        self, name: str, spec: ContainerUpdate, user: Principal, existing: dict | None = None
    ) -> tuple[ContainerResponse, int]:
        """Apply an update to a container (runs in the background after accept).

        Args:
            name: The workload name.
            spec: The container update request.
            user: The authenticated caller.
            existing: The workload state preloaded by accept_update, if any; a
                fresh fetch is done when None.

        Returns:
            The response body and HTTP status code.
        """
        group = spec.group
        oname = object_name(name, group)
        # accept_update already fetched (and authorized) this; reuse it to avoid a
        # second multi-site fanout. Falls back to a fresh fetch for direct callers.
        if existing is None:
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
        """Get one container with live per-site status (see WorkloadService.get)."""
        return await self._engine.get(OFFERING_CONTAINER, name, user, group)

    async def list(self, group: str, user: Principal, sort: str = "name") -> list[WorkloadSummary]:
        """List the group's containers (see WorkloadService.list)."""
        return await self._engine.list(OFFERING_CONTAINER, user, group, sort)

    async def delete(self, name: str, group: str, user: Principal) -> None:
        """Delete a container and its derived resources (see WorkloadService.delete)."""
        await self._engine.delete(OFFERING_CONTAINER, name, user, group)
