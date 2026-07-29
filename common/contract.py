"""The build contract shared by the API (client) and the builder service.

Both sides import these types, so the request/response shape can't drift between
the caller and the builder. The concrete build backend (func/buildpacks, a
Tekton pipeline, a remote builder microservice) implements :class:`Builder`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from common.cluster import Cluster


@dataclass
class BuildRequest:
    """Inputs for building a function image from source.

    Attributes:
        name: Workload name.
        group: Owning group.
        git_url: Source repository URL.
        branch: Branch to build.
        git_token: The caller's git token, stored for kpack to clone with.
        runtime: Function runtime (python/go/node).
        owner: Creating username, stamped on the build objects' labels.
        revision: Exact commit to build. None means build the branch head,
            which is what create/update do; the webhook path will pin the
            pushed SHA here so a rebuild is idempotent.
    """

    name: str
    group: str
    git_url: str
    branch: str
    git_token: str
    runtime: str
    owner: str = ""
    revision: str | None = None

    @property
    def build_revision(self) -> str:
        """The git revision to build: the pinned commit, else the branch."""
        return self.revision or self.branch


@dataclass
class BuildStatus:
    """A function's current build state, read back from the build backend.

    Attributes:
        state: ``Building`` / ``Ready`` / ``Failed`` / ``Unknown``.
        image: The last successfully built image, when known. May lag ``state``:
            a failed rebuild still reports the previous good image.
        message: Why the build failed, when it did.
    """

    state: str
    image: str | None = None
    message: str | None = None


class Builder(Protocol):
    """Declares function builds and reports their state."""

    @property
    def pull_secret(self) -> str:
        """The registry Secret a built function's KSVC pulls its image with."""
        ...

    def image_ref(self, req: BuildRequest) -> str:
        """The image reference the build will push to (deterministic, no I/O)."""
        ...

    def manifests(self, req: BuildRequest, labels: dict[str, str]) -> tuple[str, list[dict]]:
        """The manifests declaring the build, and the tag they push to.

        Pure: returning does not mean an image exists, or even that anything has
        been applied. A backend that builds asynchronously (kpack) describes the
        desired state and lets the caller apply it as owned resources of the
        workload.

        Args:
            req: The build request.
            labels: Ownership labels to stamp on each manifest.

        Returns:
            The image tag, and the manifests in dependency order.
        """
        ...

    def status(self, cluster: Cluster, name: str, group: str) -> BuildStatus | None:
        """The build state on one cluster, or None if it has no build for this workload."""
        ...


def image_reference(registry_base: str, req: BuildRequest) -> str:
    """The image reference convention for a build: ``{base}/{group}/{name}:{branch}``.

    Shared so the API and the builder agree on where a build's image lands.

    Args:
        registry_base: Registry host, plus organization when the registry has
            one (``RegistryConfig.base``).
        req: The build request.

    Returns:
        The fully-qualified image reference.
    """
    base = registry_base.rstrip("/")
    return f"{base}/{req.group}/{req.name}:{req.branch}"
