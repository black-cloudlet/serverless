"""Function build via Knative Functions (func) + Cloud Native Buildpacks.

In the airgapped cluster, the build uses the mirrored buildpack builder/run
images for Python/Go/JS (docs §3.1, §9). The source is cloned with the
caller-supplied git token (used transiently, never persisted — §7.2) and the
resulting image is pushed to the internal registry.

The concrete in-cluster build invocation (a `func` build or a Tekton pipeline)
is wired at deploy time; this module owns image-reference conventions and the
build contract so the rest of the app is decoupled from the build backend.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.core.config import RegistryConfig
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class BuildRequest:
    """Inputs for building a function image from source.

    Attributes:
        name: Workload name.
        group: Owning group.
        git_url: Source repository URL.
        branch: Branch to build.
        git_token: Transient git token (used to clone, never persisted).
        runtime: Function runtime (python/go/javascript).
    """

    name: str
    group: str
    git_url: str
    branch: str
    git_token: str
    runtime: str


@dataclass
class BuildResult:
    """The result of a build: the pushed image and (optionally) its digest.

    Attributes:
        image: The pushed image reference.
        digest: The immutable digest, when known (deployed for cross-site parity).
    """

    image: str
    digest: str | None = None


class Builder(Protocol):
    """Builds a function image from source and pushes it to the registry."""

    def build(self, req: BuildRequest) -> BuildResult:
        """Build and push the image for ``req``.

        Args:
            req: The build request.

        Returns:
            The build result (image and digest).
        """
        ...


class FuncBuilder:
    """Default builder targeting `func`/buildpacks against the internal registry."""

    def __init__(self, registry: RegistryConfig):
        """Initialize the builder.

        Args:
            registry: The internal registry images are pushed to.
        """
        self._registry = registry

    def image_ref(self, req: BuildRequest) -> str:
        """The image reference for a build: ``{registry}/{group}/{name}:{branch}``.

        Args:
            req: The build request.

        Returns:
            The fully-qualified image reference.
        """
        registry = self._registry.url.rstrip("/")
        return f"{registry}/{req.group}/{req.name}:{req.branch}"

    def build(self, req: BuildRequest) -> BuildResult:
        """Build the function image and push it to the internal registry.

        The build runs once and the resulting digest is deployed to every site to
        guarantee parity (docs §4). The backend invocation (func/Tekton) is
        configured per environment.

        Args:
            req: The build request.

        Returns:
            The build result (image and digest).

        Raises:
            NotImplementedError: When no build backend is wired in this
                environment.
        """
        image = self.image_ref(req)
        logger.info(
            "function build requested: name=%s runtime=%s git=%s@%s -> %s",
            req.name,
            req.runtime,
            req.git_url,
            req.branch,
            image,
        )
        raise NotImplementedError(
            "Build backend (func/Tekton) is not configured in this environment. "
            "Wire FuncBuilder.build to the in-cluster build pipeline."
        )
