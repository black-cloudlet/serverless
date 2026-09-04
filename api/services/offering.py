"""What differs between a function and a container, in one place.

:class:`~api.services.workloads.WorkloadService` is offering-agnostic: it never
branches on which offering it is serving. Everything that does differ - the
response class, which derived Secrets are pruned, the extra state an update
carries forward (a function's git token), the build status folded into a read,
the cleanup after a delete - is a member of :class:`Offering`, implemented once
per offering and passed to the engine per call.

The engine is a process-wide singleton shared by both offerings
(``api.dependencies.get_workload_service``), so the offering travels as an
argument rather than being held as state.

Implementations are stateless policy - no cluster clients, no settings - so
:data:`FUNCTION` and :data:`CONTAINER` are module singletons. Everything they
need to touch a cluster is handed to them by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from api.core.paths import webhook_url
from api.models.common import (
    ANNOTATION_GIT_COMMIT,
    ANNOTATION_RUNTIME,
    ANNOTATION_RUNTIME_VERSION,
    BuildStatusView,
    WorkloadResponse,
)
from api.models.container import ContainerResponse
from api.models.function import FunctionResponse, WebhookView
from api.services.builder import registry as registry_svc
from api.services.manifests import secrets as secret_svc
from api.services.regions import region_apply, region_read
from api.services.state import ksvc_state
from common.build import BuildBackend
from common.cluster import NamespacedCluster, ResourceKind
from common.labels import OFFERING_CONTAINER, OFFERING_FUNCTION

if TYPE_CHECKING:  # a type hint only - importing it at runtime would be a cycle
    from api.services.workloads import ApplyRequest


@dataclass(frozen=True)
class DeleteContext:
    """What :meth:`Offering.after_delete` may need, so the protocol takes one value.

    An offering is stateless policy, so everything it touches is handed to it.
    Cleanup reaches past the cluster: the registry is addressed by
    ``{group}/{name}``, so the group travels alongside the name.

    Attributes:
        cluster: The region being cleaned up, carrying its own registry.
        name: The workload name.
        group: The owning group.
    """

    cluster: NamespacedCluster
    name: str
    group: str


class Offering(Protocol):
    """The offering-specific half of a workload: what the shared engine can't know."""

    @property
    def name(self) -> str:
        """The offering label value, which is also the API kind in the URL path."""
        ...

    @property
    def response_model(self) -> type[WorkloadResponse]:
        """The response class, for the Pending 202 body the accept path returns."""
        ...

    @property
    def has_build(self) -> bool:
        """Whether this offering builds its image, so a read must fetch build state.

        False means the engine skips the build read - one thread per GET -
        instead of calling :meth:`build_status` for a None.
        """
        ...

    def applied_response(self, common: dict, req: ApplyRequest) -> WorkloadResponse:
        """Shape the response for a workload just applied, from what was submitted.

        Args:
            common: The offering-agnostic response fields the engine assembled.
            req: The apply request, carrying the offering's own submitted fields.

        Returns:
            The offering's response body.
        """
        ...

    def fetched_response(
        self, common: dict, obj: dict, spec, build: BuildStatusView | None
    ) -> WorkloadResponse:
        """Shape the response for a workload read back from a cluster.

        The values come from the stored object and its parsed spec, where
        :meth:`applied_response` takes them from the request.

        Args:
            common: The offering-agnostic response fields the engine assembled.
            obj: The KSVC read back from a representative region.
            spec: The parsed, redacted desired-state spec.
            build: The build status, when the offering has one.

        Returns:
            The offering's response body.
        """
        ...

    def managed_secrets(self, name: str) -> set[tuple[ResourceKind, str]]:
        """Derived Secrets the engine owns and must prune when unreferenced.

        Anything returned here is deleted on update unless the new spec still
        applies it, so a credential dropped from the spec cannot be orphaned.
        """
        ...

    def read_extra_state(self, cluster: NamespacedCluster, name: str) -> dict:
        """Offering-specific carried-forward state, merged into the loaded state.

        Runs off the event loop, on a region that has the workload.
        """
        ...

    def read_response_extras(
        self, cluster: NamespacedCluster, name: str, group: str, settings
    ) -> dict:
        """Offering-specific response fields that need a cluster read of their own.

        The read-path counterpart to :meth:`read_extra_state`, merged into a
        fetched response's ``common`` fields. Runs off the event loop.

        Args:
            cluster: The region to read from.
            name: The workload name.
            group: The owning group.
            settings: The API settings, for anything that has to name the API
                itself (a function's webhook URL).

        Returns:
            The extra response fields, empty for an offering with none.
        """
        ...

    def after_delete(self, ctx: DeleteContext) -> None:
        """Clean up what the KSVC's ownerReferences do not cascade to.

        Called on the local region once every region has confirmed the delete.
        """
        ...

    def build_status(
        self, builder: BuildBackend, cluster: NamespacedCluster, name: str, group: str
    ) -> BuildStatusView | None:
        """The workload's build state, or None when the offering has no build."""
        ...

    def build_states(
        self, builder: BuildBackend, cluster: NamespacedCluster, group: str
    ) -> dict[str, BuildStatusView]:
        """Build states for a whole group's listing, keyed by object name.

        The listing counterpart of :meth:`build_status`: one read for every
        workload in the group, so a list does not pay a round trip per function.
        Empty for an offering with no build.
        """
        ...


class FunctionOffering:
    """A function: built from source, so it has a build and a git credential."""

    name = OFFERING_FUNCTION
    response_model = FunctionResponse
    has_build = True

    def applied_response(self, common: dict, req: ApplyRequest) -> WorkloadResponse:
        """The function response echoing the submitted build inputs (never the token)."""
        return FunctionResponse(
            **common,
            runtime=req.runtime,
            version=req.version,
            gitRepo=req.git_url,
            revision=req.revision,
            path=req.path,
            port=req.port,
        )

    def fetched_response(
        self, common: dict, obj: dict, spec, build: BuildStatusView | None
    ) -> WorkloadResponse:
        """The function response, carrying the build the engine rolled up.

        The engine folds each region's own build into that region's status and
        holds the per-region states (see
        :func:`~api.services.state.ksvc_state.regions_with_build_status`); this
        reports the rolled-up state on ``build``.

        No image is exposed; a client reads ``gitRepo``/``revision`` instead. The
        runtime and version come from the KSVC's annotations, not the spec.
        """
        annotations = (obj.get("metadata", {}) or {}).get("annotations", {}) or {}
        return FunctionResponse(
            **common,
            runtime=annotations.get(ANNOTATION_RUNTIME),
            version=annotations.get(ANNOTATION_RUNTIME_VERSION),
            gitRepo=spec.gitRepo,
            revision=spec.revision,
            commit=annotations.get(ANNOTATION_GIT_COMMIT),
            path=spec.path,
            port=spec.port,
            build=build,
        )

    def managed_secrets(self, name: str) -> set[tuple[ResourceKind, str]]:
        """None. A function's git Secret is carried forward, never pruned.

        It is applied on every region so any of them can rebuild after a
        switchover (docs/BUILDING.md - Active/Active), and an update that omits
        the token keeps the stored copy.
        """
        return set()

    def read_extra_state(self, cluster: NamespacedCluster, name: str) -> dict:
        """The stored git and webhook tokens.

        The git token so a build-input change rebuilds without one being
        re-sent; the webhook token so a push can be authenticated
        (docs/FUNCTIONS.md - Git webhook).
        """
        git = region_read.secret_text(cluster, secret_svc.git_secret_name(name))
        hook = region_read.secret_text(cluster, secret_svc.webhook_secret_name(name))
        return {
            "git_token": git.get(secret_svc.GIT_TOKEN_KEY),
            "webhook_token": hook.get(secret_svc.WEBHOOK_TOKEN_KEY),
        }

    def read_response_extras(
        self, cluster: NamespacedCluster, name: str, group: str, settings
    ) -> dict:
        """How to configure a push to build this function, for the full GET.

        The token is shown, unlike every other stored credential: it is the
        platform's own, and a caller who can read this function can already
        build it with their bearer (docs/FUNCTIONS.md - Git webhook).
        Best-effort - an unreadable Secret leaves ``webhook`` null.

        Args:
            cluster: The region to read the Secret from.
            name: The workload name.
            group: The owning group, for the URL.
            settings: The API settings, for the absolute webhook URL.

        Returns:
            ``{"webhook": WebhookView}``, or ``{}`` when there is no token.
        """
        try:
            hook = region_read.secret_text(cluster, secret_svc.webhook_secret_name(name))
        except Exception:  # noqa: BLE001 - a read of the function must not fail on this
            return {}
        token = hook.get(secret_svc.WEBHOOK_TOKEN_KEY)
        if not token:
            return {}
        return {"webhook": WebhookView(url=webhook_url(settings, group, name), token=token)}

    def after_delete(self, ctx: DeleteContext) -> None:
        """Remove one region's build objects, then the repositories it pushed to.

        Called once per region, because both leftovers are per region: each built its
        own image into its own registry. The build objects normally cascade with
        the KSVC and this is the sweep for ones that did not; the registry has no
        owner at all - no Kubernetes object references a repository - so it is
        deleted by name (docs/BUILDING.md - Registry cleanup on delete).

        Where several regions share one registry the repository delete repeats and
        the second call 404s, which :func:`delete_repositories` already tolerates.
        """
        region_apply.delete_build_objects(ctx.cluster, ctx.name)
        registry_svc.delete_function_repositories(ctx.cluster.registry, ctx.group, ctx.name)

    def build_status(
        self, builder: BuildBackend, cluster: NamespacedCluster, name: str, group: str
    ) -> BuildStatusView | None:
        """The function's build state from the local region, or None if it has none.

        One region builds and it is always the local one (docs/BUILDING.md),
        including for a function deployed only elsewhere, so the Image is here
        whenever it exists at all - no cross-region read.

        Never an error: an unreadable build backend leaves ``build`` unset and
        the KSVC status is still reported.
        """
        status = builder.status(cluster, name, group)
        if status is None:
            return None
        return BuildStatusView(state=status.state, message=status.message)

    def build_states(
        self, builder: BuildBackend, cluster: NamespacedCluster, group: str
    ) -> dict[str, BuildStatusView]:
        """Every function's build state in the group, from the local region's Images.

        Same region as :meth:`build_status`, in one read for the whole group.
        """
        return {
            workload: BuildStatusView(state=status.state, message=status.message)
            for workload, status in builder.statuses(cluster, group).items()
        }


class ContainerOffering:
    """A container: a pre-built image, so it has a pull credential and no build."""

    name = OFFERING_CONTAINER
    response_model = ContainerResponse
    has_build = False

    def applied_response(self, common: dict, req: ApplyRequest) -> WorkloadResponse:
        """The container response echoing the submitted image and port."""
        return ContainerResponse(**common, image=req.image, port=req.port)

    def fetched_response(
        self, common: dict, obj: dict, spec, build: BuildStatusView | None
    ) -> WorkloadResponse:
        """The container response, with the image read back off the KSVC."""
        return ContainerResponse(
            **common,
            image=ksvc_state.extract_image(obj),
            registryUsername=spec.registryUsername,
            port=spec.port,
        )

    def managed_secrets(self, name: str) -> set[tuple[ResourceKind, str]]:
        """The image-pull Secret, so dropping the registry creds deletes it."""
        return {(ResourceKind.SECRET, secret_svc.pull_secret_name(name))}

    def read_extra_state(self, cluster: NamespacedCluster, name: str) -> dict:
        """None. The registry credential is read by the shared state loader."""
        return {}

    def read_response_extras(
        self, cluster: NamespacedCluster, name: str, group: str, settings
    ) -> dict:
        """None. A container is deployed from an image, so no push can build it."""
        return {}

    def after_delete(self, ctx: DeleteContext) -> None:
        """Nothing. Every container resource is owned by the KSVC and cascades.

        The image is left in the registry: it is the caller's, not the
        platform's (docs/BUILDING.md - Registry cleanup on delete).
        """

    def build_status(
        self, builder: BuildBackend, cluster: NamespacedCluster, name: str, group: str
    ) -> BuildStatusView | None:
        """None. A container is deployed from an image the caller already built."""
        return None

    def build_states(
        self, builder: BuildBackend, cluster: NamespacedCluster, group: str
    ) -> dict[str, BuildStatusView]:
        """Empty. A container has no build to fold into a listing."""
        return {}


FUNCTION: Offering = FunctionOffering()
CONTAINER: Offering = ContainerOffering()
