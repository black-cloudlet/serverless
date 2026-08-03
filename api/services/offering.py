"""What differs between a function and a container, in one place.

:class:`~api.services.workloads.WorkloadService` calls itself offering-agnostic,
and this is what makes that true. It used to be aspirational: seven ``if offering
== "function"`` branches were spread through the engine - picking a response
class (three times), pruning a container's pull Secret, reading a function's git
token, folding in its build status, and deleting its build objects. Adding a
third offering meant finding all seven.

They are now the members of :class:`Offering`, implemented once per offering and
passed to the engine per call. The engine is a process-wide singleton shared by
both offerings (``api.dependencies.get_workload_service``), which is exactly why
the offering travels as an argument rather than being held as state.

Implementations are stateless policy - no cluster clients, no settings - so
:data:`FUNCTION` and :data:`CONTAINER` are module singletons. Everything they
need to touch a cluster is handed to them by the caller.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from api.models.common import (
    ANNOTATION_RUNTIME,
    ANNOTATION_RUNTIME_VERSION,
    BuildStatusView,
    WorkloadResponse,
)
from api.models.container import ContainerResponse
from api.models.function import FunctionResponse
from api.services import ksvc_state, site_apply, site_read
from api.services import secrets as secret_svc
from common.build import BuildBackend
from common.cluster import Cluster, ResourceKind
from common.labels import OFFERING_CONTAINER, OFFERING_FUNCTION

if TYPE_CHECKING:  # a type hint only - importing it at runtime would be a cycle
    from api.services.workloads import ApplyRequest


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

        Declared rather than inferred, so the engine can skip the build read
        entirely instead of calling :meth:`build_status` just to be handed None -
        that read runs in its own thread on every GET.
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

        Distinct from :meth:`applied_response` because the values come from
        elsewhere: the stored object and its parsed spec, not the request.

        Args:
            common: The offering-agnostic response fields the engine assembled.
            obj: The KSVC read back from a representative site.
            spec: The parsed, redacted desired-state spec.
            build: The build status, when the offering has one.

        Returns:
            The offering's response body.
        """
        ...

    def managed_secrets(self, oname: str) -> set[tuple[ResourceKind, str]]:
        """Derived Secrets the engine owns and must prune when unreferenced.

        Anything returned here is deleted on update unless the new spec still
        applies it, so a credential dropped from the spec cannot be orphaned.
        """
        ...

    def read_extra_state(self, cluster: Cluster, oname: str) -> dict:
        """Offering-specific carried-forward state, merged into the loaded state.

        Runs off the event loop, on a site that has the workload.
        """
        ...

    def after_delete(self, cluster: Cluster, oname: str) -> None:
        """Clean up what the KSVC's ownerReferences do not cascade to.

        Called on the local site once every site has confirmed the delete.
        """
        ...

    def build_status(
        self, builder: BuildBackend, cluster: Cluster, name: str, group: str
    ) -> BuildStatusView | None:
        """The workload's build state, or None when the offering has no build."""
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
            branch=req.branch,
            path=req.path,
            port=req.port,
        )

    def fetched_response(
        self, common: dict, obj: dict, spec, build: BuildStatusView | None
    ) -> WorkloadResponse:
        """The function response, with the build folded into the status rollup.

        No image is exposed: the built image is an internal artifact, so a client
        reads ``gitRepo``/``branch`` instead. The runtime and version come from
        the KSVC's annotations rather than the spec - they are build inputs, not
        anything visible in the running container.
        """
        annotations = (obj.get("metadata", {}) or {}).get("annotations", {}) or {}
        return FunctionResponse(
            **{
                **common,
                "overallStatus": ksvc_state.with_build_status(common["overallStatus"], build),
            },
            runtime=annotations.get(ANNOTATION_RUNTIME),
            version=annotations.get(ANNOTATION_RUNTIME_VERSION),
            gitRepo=spec.gitRepo,
            branch=spec.branch,
            path=spec.path,
            port=spec.port,
            build=build,
        )

    def managed_secrets(self, oname: str) -> set[tuple[ResourceKind, str]]:
        """None. A function's git Secret is carried forward, never pruned.

        It is applied on every site so any of them can rebuild after a switchover
        (docs/BUILDING.md - Active/Active), and an update that omits the token
        keeps the stored copy - so pruning it would destroy the only thing that
        can rebuild the function.
        """
        return set()

    def read_extra_state(self, cluster: Cluster, oname: str) -> dict:
        """The stored git token, so a build-input change can rebuild without one."""
        git = site_read.secret_text(cluster, secret_svc.git_secret_name(oname))
        return {"git_token": git.get(secret_svc.GIT_TOKEN_KEY)}

    def after_delete(self, cluster: Cluster, oname: str) -> None:
        """Remove the build objects, which nothing cascades to on a non-running site."""
        site_apply.delete_build_objects(cluster, oname)

    def build_status(
        self, builder: BuildBackend, cluster: Cluster, name: str, group: str
    ) -> BuildStatusView | None:
        """The function's build state from the local site, or None if it has none.

        One site builds and it is always the local one (docs/BUILDING.md), including
        for a function deployed only elsewhere - so the Image is here whenever it
        exists anywhere, and a cross-site fan-out would add latency to every GET for
        something that cannot be found anywhere else.

        Never an error: a function whose image already exists must still report its
        KSVC status when the build backend cannot be read.
        """
        status = builder.status(cluster, name, group)
        if status is None:
            return None
        return BuildStatusView(state=status.state, message=status.message)


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

    def managed_secrets(self, oname: str) -> set[tuple[ResourceKind, str]]:
        """The image-pull Secret, so dropping the registry creds deletes it."""
        return {(ResourceKind.SECRET, secret_svc.pull_secret_name(oname))}

    def read_extra_state(self, cluster: Cluster, oname: str) -> dict:
        """None. The registry credential is read by the shared state loader."""
        return {}

    def after_delete(self, cluster: Cluster, oname: str) -> None:
        """Nothing. Every container resource is owned by the KSVC and cascades."""

    def build_status(
        self, builder: BuildBackend, cluster: Cluster, name: str, group: str
    ) -> BuildStatusView | None:
        """None. A container is deployed from an image the caller already built."""
        return None


FUNCTION: Offering = FunctionOffering()
CONTAINER: Offering = ContainerOffering()
