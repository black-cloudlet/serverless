"""Container workloads: deploy a supplied image (+ pull secret) via the shared engine."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

from cloudlet_apis.auth import Principal

from api.models.common import (
    PodLogSnapshot,
    PodRoster,
    WorkloadStatsResponse,
    WorkloadSummary,
)
from api.models.container import ContainerCreate, ContainerResponse, ContainerUpdate
from api.services.manifests import secrets as secret_svc
from api.services.offering import CONTAINER
from api.services.state import describe as describe_svc
from api.services.streams.sse import StreamEvent
from api.services.workloads import ApplyRequest, WorkloadService
from common.errors import ValidationError
from common.labels import OFFERING_CONTAINER, workload_labels
from common.names import object_name


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
            port=spec.port,
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
        """Reject a registry-username change made without a token (synchronous 400).

        A username with no token is a keep - it must match the stored username. A
        different one can't rotate the credential (there's no token to write), so
        it's rejected rather than silently ignored. Only enforced when the stored
        username is known; if it couldn't be read we fall through to carry-forward.
        """
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
                pull_stamp=existing.get("pull_stamp"),
            ),
            CONTAINER,
        )
        body.registryUsername = spec.registryUsername
        return body, code

    async def accept_pull(
        self, group: str, name: str, user: Principal, background
    ) -> ContainerResponse:
        """Validate and accept a pull request, scheduling the stamp (202).

        Args:
            group: The owning group (from the request path).
            name: The workload name.
            user: The authenticated caller.
            background: FastAPI background tasks to schedule the stamp on.

        Returns:
            A Pending response with a ``statusUrl`` to poll.

        Raises:
            ValidationError: If the workload runs a digest, which cannot get newer.
        """
        existing = await self._engine.load_existing(name, CONTAINER, user, group)
        image = existing.get("image") or ""
        if "@sha256:" in image:
            raise ValidationError(
                f"container '{name}' runs a digest ({image}); there is no newer image to "
                "pull. Send a PUT with a tag to track one."
            )
        background.add_task(self._engine.run, self.pull, group, name)
        # Same fallback as the function rebuild path: a workload written before the
        # host annotation existed still has a host, and it is the derived one - so
        # derive it rather than answering the 202 with an empty `hostname`.
        host = existing.get("host") or self._engine.host_for(name, None, group)
        return self._engine.accepted(CONTAINER, name, group, host, image=image)

    async def pull(self, group: str, name: str) -> None:
        """Cut a revision so Knative re-resolves the tag (runs in the background).

        Args:
            group: The owning group.
            name: The workload name.
        """
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        await self._engine.stamp_pull(name, group, stamp)

    async def get(self, name: str, group: str, user: Principal) -> ContainerResponse:
        """Get one container with live per-site status.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.

        Returns:
            The full single-container response.
        """
        return await self._engine.get(CONTAINER, name, user, group)

    async def stats(self, name: str, group: str, user: Principal) -> WorkloadStatsResponse:
        """Read the container's live state (the poll view).

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.

        Returns:
            The rollup plus per-site replicas and usage.
        """
        return await self._engine.stats(CONTAINER, name, user, group)

    async def pods(self, name: str, group: str, user: Principal) -> PodRoster:
        """The container's pods on the current site, read once.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.

        Returns:
            The roster.
        """
        return await self._engine.pods(CONTAINER, name, user, group)

    async def pod_logs(
        self,
        name: str,
        group: str,
        user: Principal,
        *,
        pod: str,
        container: str,
        since_seconds: int | None,
        limit_bytes: int | None,
    ) -> PodLogSnapshot:
        """One of the container's pods' logs as it stands, read once.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.
            pod: The pod to read.
            container: The pod container to read.
            since_seconds: Only lines newer than this, if set.
            limit_bytes: Cap on the bytes read, if set.

        Returns:
            The snapshot.
        """
        return await self._engine.pod_logs(
            CONTAINER,
            name,
            user,
            group,
            pod=pod,
            container=container,
            since_seconds=since_seconds,
            limit_bytes=limit_bytes,
        )

    async def stream_pods(
        self, name: str, group: str, user: Principal, *, interval: float | None
    ) -> AsyncIterator[StreamEvent]:
        """Stream which pods the container has on the current site.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.
            interval: Seconds between listings; None takes the default.

        Returns:
            The event stream.
        """
        return await self._engine.stream_pods(CONTAINER, name, user, group, interval=interval)

    async def stream_pod_logs(
        self,
        name: str,
        group: str,
        user: Principal,
        *,
        pod: str,
        container: str,
        since_seconds: int | None,
    ) -> AsyncIterator[StreamEvent]:
        """Follow one of the container's pods' logs, on the current site.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.
            pod: The pod to follow.
            container: The pod container to read.
            since_seconds: How far back the log starts, if set.

        Returns:
            The event stream.
        """
        return await self._engine.stream_pod_logs(
            CONTAINER,
            name,
            user,
            group,
            pod=pod,
            container=container,
            since_seconds=since_seconds,
        )

    async def stream_stats(
        self, name: str, group: str, user: Principal, *, interval: float | None
    ) -> AsyncIterator[StreamEvent]:
        """Push the container's live state on an interval.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.
            interval: Seconds between readings; None takes the default.

        Returns:
            The event stream.
        """
        return await self._engine.stream_stats(CONTAINER, name, user, group, interval=interval)

    async def list(self, group: str, user: Principal, sort: str = "name") -> list[WorkloadSummary]:
        """List the group's containers.

        Args:
            group: The owning group.
            user: The authenticated caller.
            sort: Sort key, "name" or "createdAt".

        Returns:
            The per-workload summaries.
        """
        return await self._engine.list(CONTAINER, user, group, sort)

    async def delete(self, name: str, group: str, user: Principal) -> None:
        """Delete a container and its derived resources.

        Args:
            name: The workload name.
            group: The owning group.
            user: The authenticated caller.
        """
        await self._engine.delete(CONTAINER, name, user, group)
