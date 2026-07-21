"""Function request schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from api.models.common import (
    EnvVar,
    FileMount,
    Hostname,
    Name,
    Scaling,
    WorkloadResponse,
    WorkloadSize,
)


class FunctionCreate(BaseModel):
    """Request body to create a function from source (built into an image).

    The owning group comes from the request path (``/api/v1/groups/{group}/...``),
    not the body. ``runtime`` is a free string here; the set of valid runtimes is
    data (a mounted ConfigMap, see services.runtimes) so it is validated against
    the live registry in the service layer, not as a fixed enum. GET
    /api/v1/functions/info lists the accepted values.
    """

    name: Name
    gitRepo: str
    branch: str = "main"
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

    Mirrors ``ContainerUpdate``: non-secret fields are replaced (an omitted one
    reverts to its default), so the build inputs ``gitRepo`` and ``runtime`` are
    required just like on create (``branch`` resets to ``main``). The only
    keep-on-omit is the git token, which - like a redacted secret - can't be read
    back: it's stored (``{workload}-git`` Secret) and reused unless the client
    sends a new one (which rotates it). The service rebuilds from source only when
    a build input actually changes or the token is rotated - a config-only edit
    that re-sends the same build inputs keeps the current image.
    """

    gitRepo: str
    branch: str = "main"
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
