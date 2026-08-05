"""Container workloads: deploy a supplied image (+ pull secret) via the shared engine."""

from __future__ import annotations

from api.auth.claims import Principal
from api.models.common import LogsResponse, WorkloadStatsResponse, WorkloadSummary
from api.models.container import ContainerCreate, ContainerResponse, ContainerUpdate
from api.services.manifests import secrets as secret_svc
from api.services.offering import CONTAINER
from api.services.state import describe as describe_svc
from api.services.workloads import ApplyRequest, WorkloadService
from common.errors import ValidationError
from common.labels import OFFERING_CONTAINER, workload_labels
from common.names import object_name


class ContainerService:
    """Container-specific orchestration; delegates the shared work to WorkloadService."""

    def __init__(self, engine: WorkloadService):
        """Initialize the service."""
        self._engine = engine

    def _echo(self, spec) -> dict:
        """Submitted config echoed back on the spec (secrets redacted)."""
        return dict(
            size=spec.size,
            scaling=spec.scaling,
            env=describe_svc.redact_env(spec.env),
            files=describe_svc.redact_files(spec.files),
            registryUsername=spec.registryUsername,
            port=spec.port,
        )

    async def accept(
        self, group: str, spec: ContainerCreate, user: Principal, background
    ) -> ContainerResponse:
        """Validate and accept a create request, scheduling the deploy (202)."""
        return await self._engine.accept_create(
            offering=CONTAINER,
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
        """Validate and accept an update request, scheduling the deploy (202)."""
        return await self._engine.accept_update(
            offering=CONTAINER,
            group=group,
            name=name,
            spec=spec,
            user=user,
            background=background,
            work=self.update,
            pre_check=self._check_registry_change,
            image=spec.image,
            **self._echo(spec),
        )

    @staticmethod
    def _check_registry_change(spec: ContainerUpdate, existing: dict) -> None:
        """Reject a registry-username change made without a token (synchronous 400)."""
        stored = existing.get("registry_username")
        if (
            spec.registryToken is None
            and spec.registryUsername is not None
            and stored is not None
            and spec.registryUsername != stored
        ):
            raise ValidationError(
                "changing registryUsername requires registryToken "
                "(send both to rotate the credential)"
            )

    async def create(
        self, group: str, spec: ContainerCreate, user: Principal
    ) -> tuple[ContainerResponse, int]:
        """Deploy a new container (runs in the background after accept).

        Builds the pull secret (if registry creds were given) and applies the
        workload to all target sites.
        """
        oname = object_name(spec.name, group)
        # Registry creds are optional (public image -> no pull secret).
        pull_name: str | None = None
        pull: dict | None = None
        if spec.registryUsername and spec.registryToken:
            pull_name = secret_svc.pull_secret_name(oname)
            pull = secret_svc.build_pull_secret(
                pull_name,
                workload_labels(group, user.username, oname, OFFERING_CONTAINER),
                secret_svc.registry_of(spec.image),  # key to the image's registry
                spec.registryUsername,
                spec.registryToken,
            )

        # No absence probe here: apply_workload runs one combined host+absence pass
        # over the same targets immediately before it mutates, which is both a
        # stronger guard (nothing happens in between) and one fewer cross-site trip.
        body, code = await self._engine.apply_workload(
            ApplyRequest(
                name=spec.name,
                user=user,
                group=group,
                image=spec.image,
                env=spec.env,
                files=spec.files,
                scaling=spec.scaling,
                size=spec.size,
                port=spec.port,
                hostname=spec.hostname,
                sites=spec.sites,
                pull_secret_name=pull_name,
                pull_secret_manifest=pull,
                created=True,
            ),
            CONTAINER,
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
        """Apply an update to a container (runs in the background after accept)."""
        oname = object_name(name, group)
        # accept_update already fetched (and authorized) this; reuse it to avoid a
        # second multi-site fanout. Falls back to a fresh fetch for direct callers.
        if existing is None:
            existing = await self._engine.load_existing(name, CONTAINER, user, group)
        image = spec.image
        port = spec.port

        # token -> rotate; username only -> keep, re-keyed to the image's registry;
        # neither -> remove (docs/ARCHITECTURE.md - Secrets).
        labels = workload_labels(group, user.username, oname, OFFERING_CONTAINER)
        pull_name = secret_svc.pull_secret_name(oname)
        pull: dict | None = None
        if spec.registryToken:  # rotate
            pull = secret_svc.build_pull_secret(
                pull_name,
                labels,
                secret_svc.registry_of(image),
                spec.registryUsername,
                spec.registryToken,
            )
        elif spec.registryUsername and existing.get("registry_token"):  # keep: re-key
            pull = secret_svc.build_pull_secret(
                pull_name,
                labels,
                secret_svc.registry_of(image),
                existing.get("registry_username"),
                existing["registry_token"],
            )
        elif spec.registryUsername and existing.get("pull_secret"):
            pull_name = existing["pull_secret"]  # keep, but token unreadable: carry forward
        else:
            pull_name = None  # remove -> public

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
                port=port,
                hostname=spec.hostname,
                sites=None,
                pull_secret_name=pull_name,
                pull_secret_manifest=pull,
                created=False,
                prev_host=existing.get("host"),
                kept_env=existing.get("env_values"),
                kept_files=existing.get("files_values"),
            ),
            CONTAINER,
        )
        body.registryUsername = spec.registryUsername
        return body, code

    async def get(self, name: str, group: str, user: Principal) -> ContainerResponse:
        """Get one container with live per-site status."""
        return await self._engine.get(CONTAINER, name, user, group)

    async def stats(self, name: str, group: str, user: Principal) -> WorkloadStatsResponse:
        """Read the container's live state (the poll view)."""
        return await self._engine.stats(CONTAINER, name, user, group)

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
        """Snapshot the container's pod logs from the current site."""
        return await self._engine.logs(
            CONTAINER,
            name,
            user,
            group,
            container=container,
            since_seconds=since_seconds,
            limit_bytes=limit_bytes,
        )

    async def list(self, group: str, user: Principal, sort: str = "name") -> list[WorkloadSummary]:
        """List the group's containers."""
        return await self._engine.list(CONTAINER, user, group, sort)

    async def delete(self, name: str, group: str, user: Principal) -> None:
        """Delete a container and its derived resources."""
        await self._engine.delete(CONTAINER, name, user, group)
