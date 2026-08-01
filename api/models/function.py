"""Function request schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from api.models.common import (
    Branch,
    BuildStatusView,
    EnvVar,
    FileMount,
    GitUrl,
    Hostname,
    Name,
    Scaling,
    SourcePath,
    WorkloadResponse,
    WorkloadSize,
)


class FunctionCreate(BaseModel):
    """Request body to create a function from source (built into an image).

    The owning group comes from the request path, not the body. ``runtime`` is a
    free string here: the valid set is data (a ConfigMap), so it is checked against
    the live registry in the service layer rather than as a fixed enum.
    """

    name: Name
    gitRepo: GitUrl
    branch: Branch = "main"
    path: SourcePath = ""
    gitToken: str
    runtime: str
    env: list[EnvVar] = Field(default_factory=list)
    files: list[FileMount] = Field(default_factory=list)
    scaling: Scaling = Field(default_factory=Scaling)
    size: WorkloadSize = "small"  # resource size; see services.ksvc
    sites: list[str] | None = None
    # Optional custom external host; defaults to {name}-{group}.{route_domain}.
    hostname: Hostname | None = None


class FunctionUpdate(BaseModel):
    """Replace the mutable spec: the body is the full desired state.

    Mirrors ``ContainerUpdate``: non-secret fields are replaced, so ``gitRepo`` and
    ``runtime`` are required as on create. Only the git token is keep-on-omit; it is
    stored and reused unless the client sends a new one, which rotates it. A rebuild
    happens only when a build input changes or the token rotates.
    """

    gitRepo: GitUrl
    branch: Branch = "main"
    path: SourcePath = ""
    gitToken: str | None = None  # keep-on-omit: reuses the stored token
    runtime: str
    env: list[EnvVar] = Field(default_factory=list)
    files: list[FileMount] = Field(default_factory=list)
    scaling: Scaling = Field(default_factory=Scaling)
    size: WorkloadSize = "small"
    hostname: Hostname | None = None


class FunctionResponse(WorkloadResponse):
    """A function, shaped like FunctionCreate (gitToken redacted) + live status.

    No image is exposed: the built image is an internal artifact - the client
    deals in source (gitRepo/branch), not images.
    """

    type: Literal["function", "container"] = "function"
    runtime: str | None = None
    gitRepo: str | None = None
    branch: str | None = None
    path: str | None = None
    # Present once the function has an Image on the local site; None on a site
    # that has never built it (e.g. straight after a switchover).
    build: BuildStatusView | None = None
