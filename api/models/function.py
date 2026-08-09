"""Function request schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from api.models.common import (
    DEFAULT_PORT,
    PORT_MAX,
    PORT_MIN,
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
    # repr=False: a credential must not ride along into log lines, tracebacks
    # or validation errors that print the spec.
    gitToken: str = Field(repr=False)
    runtime: str
    # One of the runtime's advertised `versions` (GET /api/v1/functions/info).
    # Omitted means the platform default for that runtime - never the
    # buildpack's own default, which drifts with the buildpackage.
    version: str | None = None
    env: list[EnvVar] = Field(default_factory=list)
    files: list[FileMount] = Field(default_factory=list)
    scaling: Scaling = Field(default_factory=Scaling)
    size: WorkloadSize = "small"  # resource size; see services.ksvc
    sites: list[str] | None = None
    # Optional custom external host; defaults to {name}-{group}.{route_domain}.
    hostname: Hostname | None = None
    # Identical to a container's, deliberately: an app either serves on 8080 or
    # it does not, and which offering built it changes nothing. A buildpack app
    # that reads $PORT gets 8080 either way; one that hardcodes another port can
    # say so instead of never becoming ready.
    port: int = Field(default=DEFAULT_PORT, ge=PORT_MIN, le=PORT_MAX)


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
    # keep-on-omit: reuses the stored token. repr=False as on create.
    gitToken: str | None = Field(default=None, repr=False)
    runtime: str
    # Replaced like every other non-secret field, NOT keep-on-omit: omitting it
    # returns the function to the platform default for its runtime, and rebuilds.
    version: str | None = None
    env: list[EnvVar] = Field(default_factory=list)
    files: list[FileMount] = Field(default_factory=list)
    scaling: Scaling = Field(default_factory=Scaling)
    size: WorkloadSize = "small"
    hostname: Hostname | None = None
    # Replaced, NOT keep-on-omit - the same rule `version` follows: omitting it
    # returns the function to 8080 rather than keeping the port deployed. Only
    # secret material is keep-on-omit, because only it cannot be read back.
    port: int = Field(default=DEFAULT_PORT, ge=PORT_MIN, le=PORT_MAX)


class FunctionResponse(WorkloadResponse):
    """A function, shaped like FunctionCreate (gitToken redacted) + live status.

    No image is exposed: the built image is an internal artifact - the client
    deals in source (gitRepo/branch), not images.
    """

    type: Literal["function", "container"] = "function"
    runtime: str | None = None
    # The version the caller asked for, or None when they took the default. The
    # default itself is on GET /api/v1/functions/info, so a client can resolve
    # what None means without this echoing a value nobody submitted.
    version: str | None = None
    gitRepo: str | None = None
    branch: str | None = None
    path: str | None = None
    # Present once the function has an Image on the local site; None on a site
    # that has never built it (e.g. straight after a switchover).
    build: BuildStatusView | None = None
    port: int | None = None  # explicit container port, or None for Knative's default
