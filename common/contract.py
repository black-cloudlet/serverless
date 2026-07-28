"""The build contract shared by the API (client) and the builder service.

Both sides import these types, so the request/response shape can't drift between
the caller and the builder. The concrete build backend (func/buildpacks, a
Tekton pipeline, a remote builder microservice) implements :class:`Builder`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class BuildRequest:
    """Inputs for building a function image from source.

    Attributes:
        name: Workload name.
        group: Owning group.
        git_url: Source repository URL.
        branch: Branch to build.
        git_token: Transient git token (used to clone, never persisted).
        runtime: Function runtime (python/go/node/typescript).
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


def image_reference(registry_url: str, req: BuildRequest) -> str:
    """The image reference convention for a build: ``{registry}/{group}/{name}:{branch}``.

    Shared so the API and the builder agree on where a build's image lands.

    Args:
        registry_url: The internal registry host.
        req: The build request.

    Returns:
        The fully-qualified image reference.
    """
    registry = registry_url.rstrip("/")
    return f"{registry}/{req.group}/{req.name}:{req.branch}"
