"""Container workloads: deploy a supplied image (+ pull secret) via the shared engine."""

from __future__ import annotations

from api.auth.claims import Principal
from api.models.common import LogsResponse, WorkloadSummary
from api.models.container import ContainerCreate, ContainerResponse, ContainerUpdate
from api.services import describe as describe_svc
from api.services import secrets as secret_svc
from api.services.workloads import OFFERING_CONTAINER, WorkloadService, object_name
from common.labels import workload_labels


class ContainerService:
    """Container-specific orchestration; delegates the shared work to WorkloadService."""

    def __init__(self, engine: WorkloadService):
        """Initialize the service.

        Args:
            engine: The shared workload engine doing the cross-site work.
        """
        self._engine = engine

    def _echo(self, spec) -> dict:
        """Submitted config echoed back on the spec (secrets redacted)."""
        return dict(
            size=spec.size,
            scaling=spec.scaling,
            env=describe_svc.redact_env(spec.env),
            files=describe_svc.redact_files(spec.files),
            registryUsername=spec.registryUsername,
        )

    async def accept(
        self, group: str, spec: ContainerCreate, user: Principal, background
    ) -> ContainerResponse:
        """Validate and accept a create request, scheduling the deploy (202).

        Args:
            group: The owning group (from the request path).
            spec: The container create request.
            user: The authenticated caller.
            background: FastAPI background tasks to schedule the deploy on.

        Returns:
            A Pending response with a ``statusUrl`` to poll.
        """
        return await self._engine.accept_create(
            offering=OFFERING_CONTAINER,
            group=group,
            spec=spec,
            user=user,
            background=background,
            work=self.create,
            image=spec.image,
            **self._echo(spec),
        )

    async def accept_update(
        self, group: str, name: str, spec: ContainerUpdate, user: Principal, background
    ) -> ContainerResponse:
        """Validate and accept an update request, scheduling the deploy (202).

        Args:
            group: The owning group (from the request path).
            name: The workload name.
            spec: The container update request.
            user: The authenticated caller.
            background: FastAPI background tasks to schedule the deploy on.

        Returns:
            A Pending response with a ``statusUrl`` to poll.
        """
        return await self._engine.accept_update(
            offering=OFFERING_CONTAINER,
            group=group,
            name=name,
            spec=spec,
            user=user,
            background=background,
            work=self.update,
            image=spec.image,
            **self._echo(spec),
        )

    async def create(
        self, group: str, spec: ContainerCreate, user: Principal
    ) -> tuple[ContainerResponse, int]:
        """Deploy a new container (runs in the background after accept).

        Builds the pull secret (if registry creds were given) and applies the
        workload to all target sites.

        Args:
            group: The owning group (from the request path).
            spec: The container create request.
            user: The authenticated caller.

        Returns:
            The response body and HTTP status code.
        """
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
            spec.name, group, self._engine.deployer.resolve_targets(spec.sites)
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
        self,
        group: str,
        name: str,
        spec: ContainerUpdate,
        user: Principal,
        existing: dict | None = None,
    ) -> tuple[ContainerResponse, int]:
        """Apply an update to a container (runs in the background after accept).

        Args:
            group: The owning group (from the request path).
            name: The workload name.
            spec: The container update request.
            user: The authenticated caller.
            existing: The workload state preloaded by accept_update, if any; a
                fresh fetch is done when None.

        Returns:
            The response body and HTTP status code.
        """
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
            prev_host=existing.get("host"),
            kept_env=existing.get("env_values"),
            kept_files=existing.get("files_values"),
        )
        body.registryUsername = spec.registryUsername
        return body, code

    async def get(self, name: str, group: str, user: Principal) -> ContainerResponse:
        """Get one container with live per-site status.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.

        Returns:
            The full single-container response.
        """
        return await self._engine.get(OFFERING_CONTAINER, name, user, group)

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
        """Snapshot the container's pod logs from the current site.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.
            container: The pod container to read.
            since_seconds: Only logs newer than this, if set.
            limit_bytes: Cap on bytes read per pod, if set.

        Returns:
            The container's per-pod logs from the local site.
        """
        return await self._engine.logs(
            OFFERING_CONTAINER,
            name,
            user,
            group,
            container=container,
            since_seconds=since_seconds,
            limit_bytes=limit_bytes,
        )

    async def list(self, group: str, user: Principal, sort: str = "name") -> list[WorkloadSummary]:
        """List the group's containers.

        Args:
            group: The owning group.
            user: The authenticated caller.
            sort: Sort key, "name" or "createdAt".

        Returns:
            The per-workload summaries.
        """
        return await self._engine.list(OFFERING_CONTAINER, user, group, sort)

    async def delete(self, name: str, group: str, user: Principal) -> None:
        """Delete a container and its derived resources.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.
        """
        await self._engine.delete(OFFERING_CONTAINER, name, user, group)
