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
class BuildResult:
    """The result of a build: the pushed image and (optionally) its digest.

    Attributes:
        image: The pushed image reference.
        digest: The immutable digest, when known (deployed for cross-site parity).
    """

    image: str
    digest: str | None = None


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

    def build(self, req: BuildRequest) -> BuildResult:
        """Declare the build for ``req``.

        Returning does not mean an image exists - a backend that builds
        asynchronously (kpack) records the desired state and returns the
        reference the build will push to.

        Args:
            req: The build request.

        Returns:
            The build result (image and digest).
        """
        ...

    def status(self, cluster: Cluster, name: str, group: str) -> BuildStatus | None:
        """The build state on one cluster, or None if it has no build for this workload."""
        ...

    def cleanup(self, cluster: Cluster, name: str, group: str) -> None:
        """Remove the workload's build objects from one cluster."""
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
