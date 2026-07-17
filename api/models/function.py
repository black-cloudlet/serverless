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
    the live registry in the service layer, not as a fixed enum. GET /api/v1/info
    lists the accepted values.
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
    """Full replace of the mutable spec.

    Changing any build input (gitRepo, branch, runtime) rebuilds from source; the
    git token is stored (``{workload}-git`` Secret), so the rebuild reuses it and
    the client need not re-send ``gitToken``. Sending ``gitToken`` rotates it (and
    rebuilds). Otherwise the existing image is kept and only config is updated. The
    rebuild decision lives in the service, which knows the current build inputs and
    the stored token.
    """

    # Rebuild inputs (all optional). gitRepo/branch/runtime default to the existing
    # values when omitted; the stored gitToken is reused unless a new one is sent.
    gitRepo: str | None = None
    branch: str | None = None
    gitToken: str | None = None
    runtime: str | None = None
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
