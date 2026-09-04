"""Function request schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from api.models.common import (
    DEFAULT_PORT,
    PORT_MAX,
    PORT_MIN,
    BuildStatusView,
    EnvVar,
    FileMount,
    GitUrl,
    Hostname,
    Name,
    Revision,
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
    revision: Revision = "main"
    path: SourcePath = ""
    # repr=False: a credential must not ride along into log lines, tracebacks
    # or validation errors that print the spec.
    gitToken: str = Field(repr=False)
    runtime: str
    # One of the runtime's advertised `versions` (GET /api/serverless/v1/functions/info).
    # Omitted means the platform default for that runtime, not the buildpack's
    # own default.
    version: str | None = None
    env: list[EnvVar] = Field(default_factory=list)
    files: list[FileMount] = Field(default_factory=list)
    scaling: Scaling = Field(default_factory=Scaling)
    size: WorkloadSize = "small"  # resource size; see services.ksvc
    regions: list[str] | None = None
    # Optional custom external host; defaults to {name}-{group}.{route_domain}.
    hostname: Hostname | None = None
    # Container port the built app listens on, with the same rules and default a
    # container has. A buildpack app that reads $PORT gets 8080; send another
    # port when the app hardcodes one (docs/FUNCTIONS.md - API - create & update).
    port: int = Field(default=DEFAULT_PORT, ge=PORT_MIN, le=PORT_MAX)


class FunctionUpdate(BaseModel):
    """Replace the mutable spec: the body is the full desired state.

    Mirrors ``ContainerUpdate``: non-secret fields are replaced, so ``gitRepo`` and
    ``runtime`` are required as on create. Only the git token is keep-on-omit; it is
    stored and reused unless the client sends a new one, which rotates it. A rebuild
    happens only when a build input changes or the token rotates.
    """

    gitRepo: GitUrl
    revision: Revision = "main"
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
    # secret material is keep-on-omit.
    port: int = Field(default=DEFAULT_PORT, ge=PORT_MIN, le=PORT_MAX)


class WebhookView(BaseModel):
    """Everything needed to configure a git webhook for this function.

    Returned whole so a caller configures the hook by copying two fields rather
    than assembling a URL. Unlike ``gitToken``, ``token`` is *shown*: it is the
    platform's own credential, minted here, and its only use is being pasted
    into the provider (docs/FUNCTIONS.md - Git webhook).
    """

    url: str
    # repr=False, as gitToken is: a credential must not ride along into log
    # lines or tracebacks that print the response.
    token: str = Field(repr=False)
    provider: str = "gitlab"
    events: list[str] = Field(default_factory=lambda: ["push"])


class FunctionResponse(WorkloadResponse):
    """A function, shaped like FunctionCreate (gitToken redacted) + live status.

    The built image is not part of the response; a function is described by its
    source (``gitRepo``/``revision``/``path``) and its build state.
    """

    type: Literal["function", "container"] = "function"
    runtime: str | None = None
    # The version the caller asked for, or None when they took the default. The
    # default itself is published on GET /api/serverless/v1/functions/info.
    version: str | None = None
    gitRepo: str | None = None
    revision: str | None = None
    # The commit a git push pinned, or None while the build follows `revision`.
    # Read-only: it is set by the webhook and cleared by POST .../build and PUT,
    # never sent by a client (docs/FUNCTIONS.md - Git webhook).
    commit: str | None = None
    # How to configure a push to build this function. None on a response that
    # did not read one (a rebuild's 202 carries no secret read).
    webhook: WebhookView | None = None
    path: str | None = None
    # Present once the function has an Image on the local region; None on a region
    # that has never built it (e.g. straight after a switchover).
    build: BuildStatusView | None = None
    port: int | None = None  # explicit container port, or None for Knative's default
