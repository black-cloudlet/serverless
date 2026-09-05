"""Container workloads: deploy a supplied image (+ pull secret) via the shared engine."""

from __future__ import annotations

from datetime import UTC, datetime

from cloudlet_apis.auth import Principal
from cloudlet_apis.logging import get_logger

from api.models.container import ContainerCreate, ContainerResponse, ContainerUpdate
from api.services.manifests import secrets as secret_svc
from api.services.offering import CONTAINER
from api.services.offering_service import OfferingService
from api.services.state import describe as describe_svc
from api.services.workloads import ApplyRequest
from api.services.workloads.service import run_background
from common.errors import ValidationError
from common.labels import OFFERING_CONTAINER, workload_labels
from common.names import digest_of

logger = get_logger(__name__)


class ContainerService(OfferingService):
    """Container orchestration: the image-pull Secret and the pull, over the engine.

    Create and update compose the pull Secret from the registry credential and
    hand the engine an :class:`ApplyRequest`; pull stamps every region so
    Knative resolves the tag again. Reads, streams and delete are
    :class:`OfferingService`'s.
    """

    offering = CONTAINER

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

        A username with no token is a keep, so it must match the stored
        username; a different one names a credential there is no token to write
        (docs/ARCHITECTURE.md - Customer-provided credentials). Enforced only
        when the stored username is known - when it could not be read, the
        update falls through to carry-forward.
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
        workload to all target regions.

        Args:
            group: The owning group (from the request path).
            spec: The container create request.
            user: The authenticated caller.

        Returns:
            The response body and HTTP status code.
        """
        # Registry creds are optional (public image -> no pull secret).
        pull_name: str | None = None
        pull: dict | None = None
        if spec.registryUsername and spec.registryToken:
            pull_name = secret_svc.pull_secret_name(spec.name)
            pull = secret_svc.build_pull_secret(
                pull_name,
                workload_labels(group, user.username, spec.name, OFFERING_CONTAINER),
                secret_svc.registry_of(spec.image),  # key to the image's registry
                spec.registryUsername,
                spec.registryToken,
            )

        # No absence probe here: apply_workload runs one combined host+absence
        # pass over the same targets immediately before it mutates.
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
        # accept_update already fetched (and authorized) this; reuse it to avoid a
        # second multi-region fanout. Falls back to a fresh fetch for direct callers.
        if existing is None:
            existing = await self._engine.load_existing(name, CONTAINER, user, group)
        image = spec.image
        port = spec.port

        # token -> rotate; username only -> keep, re-keyed to the image's registry;
        # neither -> remove (docs/ARCHITECTURE.md - Secrets).
        labels = workload_labels(group, user.username, name, OFFERING_CONTAINER)
        pull_name = secret_svc.pull_secret_name(name)
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
        if digest_of(image):
            raise ValidationError(
                f"container '{name}' runs a digest ({image}); there is no newer image to "
                "pull. Send a PUT with a tag to track one."
            )
        background.add_task(run_background, self.pull, group, name)
        # Falls back to the derived host when the workload carries no host
        # annotation, so the 202 never answers with an empty `hostname`. The
        # function rebuild path does the same.
        host = existing.get("host") or self._engine.host_for(name, None, group)
        return self._engine.accepted(CONTAINER, name, group, host, image=image)

    async def pull(self, group: str, name: str) -> None:
        """Cut a revision so Knative re-resolves the tag (runs in the background).

        Stamps one timestamp on every region, so they land on the same revision
        (docs/CONTAINERS.md - Pulling the tag again). Runs after
        :meth:`accept_pull` has returned the 202.

        Args:
            group: The owning group.
            name: The workload name.
        """
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        statuses = await self._engine.stamp_pull(name, group, stamp)
        # The 202 has already been sent, so a failed patch has no response left
        # to land in; the log is where it is reported.
        failed = [s for s in statuses if s.message]
        if failed:
            logger.warning(
                "pull of '%s' (group '%s') failed in %s",
                name,
                group,
                ", ".join(f"{s.region}: {s.message}" for s in failed),
            )
