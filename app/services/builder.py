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

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class BuildRequest:
    name: str
    group: str
    git_url: str
    branch: str
    git_token: str
    runtime: str


@dataclass
class BuildResult:
    image: str
    digest: str | None = None


class Builder(Protocol):
    def build(self, req: BuildRequest) -> BuildResult: ...


class FuncBuilder:
    """Default builder targeting `func`/buildpacks against the internal registry."""

    def __init__(self, settings: Settings):
        self._settings = settings

    def image_ref(self, req: BuildRequest) -> str:
        registry = self._settings.registry.url.rstrip("/")
        return f"{registry}/{req.group}/{req.name}:{req.branch}"

    def build(self, req: BuildRequest) -> BuildResult:
        # The build runs once and the resulting digest is deployed to every site
        # to guarantee parity (docs §4). Backend invocation (func/Tekton) is
        # configured per environment; see deploy docs.
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
