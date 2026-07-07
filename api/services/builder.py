"""Function build via Knative Functions (func) + Cloud Native Buildpacks.

In the airgapped cluster, the build uses the mirrored buildpack builder/run
images for Python/Go/JS (docs §3.1, §9). The source is cloned with the
caller-supplied git token (used transiently, never persisted - §7.2) and the
resulting image is pushed to the internal registry.

This is the API-side ``Builder`` (see :mod:`common.contract`). The concrete
in-cluster build invocation (a ``func`` build or a Tekton pipeline) is wired at
deploy time; when the build moves to its own microservice, a ``RemoteBuilder``
implementing the same contract replaces this one with no change to callers.
"""

from __future__ import annotations

from api.core.config import RegistryConfig
from common.contract import BuildRequest, BuildResult, image_reference
from common.logging import get_logger

logger = get_logger(__name__)


class FuncBuilder:
    """Default builder targeting `func`/buildpacks against the internal registry."""

    def __init__(self, registry: RegistryConfig):
        """Initialize the builder.

        Args:
            registry: The internal registry images are pushed to.
        """
        self._registry = registry

    def image_ref(self, req: BuildRequest) -> str:
        """The image reference for a build (see ``common.contract.image_reference``).

        Args:
            req: The build request.

        Returns:
            The fully-qualified image reference.
        """
        return image_reference(self._registry.url, req)

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
