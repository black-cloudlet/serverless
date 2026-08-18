"""Container request schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from api.models.common import (
    DEFAULT_PORT,
    PORT_MAX,
    PORT_MIN,
    EnvVar,
    FileMount,
    Hostname,
    ImageRef,
    Name,
    Scaling,
    WorkloadResponse,
    WorkloadSize,
)


class ContainerCreate(BaseModel):
    """Request body to deploy a pre-built container image.

    The owning group comes from the request path (``/api/serverless/v1/groups/{group}/...``),
    not the body.
    """

    name: Name
    image: ImageRef
    # Registry credentials are optional: omit both for a public image. When
    # supplied, both are required together (a username alone can't authenticate).
    registryUsername: str | None = None
    # repr=False: a credential must not ride along into log lines, tracebacks
    # or validation errors that print the spec.
    registryToken: str | None = Field(default=None, repr=False)
    env: list[EnvVar] = Field(default_factory=list)
    files: list[FileMount] = Field(default_factory=list)
    scaling: Scaling = Field(default_factory=Scaling)
    size: WorkloadSize = "small"  # resource size; see services.ksvc
    regions: list[str] | None = None
    # Optional custom external host; defaults to {name}-{group}.{route_domain}.
    hostname: Hostname | None = None
    # Port the image listens on. Defaults to 8080 - the port Knative injects as
    # $PORT and the one most images serve on. Send it when the image differs:
    # nothing can detect that, and the mismatch surfaces as a revision that never
    # becomes ready rather than as a rejected request.
    port: int = Field(default=DEFAULT_PORT, ge=PORT_MIN, le=PORT_MAX)

    @model_validator(mode="after")
    def _registry_creds(self) -> "ContainerCreate":
        """Require registry username and token to be provided together (or neither)."""
        if (self.registryUsername is None) != (self.registryToken is None):
            raise ValueError(
                "registryUsername and registryToken must be provided together "
                "(or both omitted for a public image)"
            )
        return self


class ContainerUpdate(BaseModel):
    """Replace the mutable spec: the body is the full desired state.

    Non-secret fields are replaced, so ``image`` is required as on create and
    ``port`` returns to its default when omitted. Only redacted secret material is
    keep-on-omit - it cannot be read back, so omitting it keeps what is stored.
    """

    image: ImageRef
    # Registry creds: username+token rotates, username-only keeps, neither removes
    # (public); a token needs a username. See docs/ARCHITECTURE.md - Secrets for the full semantics.
    registryUsername: str | None = None
    # repr=False as on create: a credential must not print with the spec.
    registryToken: str | None = Field(default=None, repr=False)
    env: list[EnvVar] = Field(default_factory=list)
    files: list[FileMount] = Field(default_factory=list)
    scaling: Scaling = Field(default_factory=Scaling)
    size: WorkloadSize = "small"
    hostname: Hostname | None = None
    # Replaced, not keep-on-omit: omitting it returns the container to 8080.
    port: int = Field(default=DEFAULT_PORT, ge=PORT_MIN, le=PORT_MAX)

    @model_validator(mode="after")
    def _registry_creds(self) -> "ContainerUpdate":
        """A registry token needs a username; a username alone keeps the existing token."""
        if self.registryToken is not None and self.registryUsername is None:
            raise ValueError("registryToken requires registryUsername")
        return self


class ContainerResponse(WorkloadResponse):
    """A container, shaped like ContainerCreate (registry token redacted) + status."""

    type: Literal["function", "container"] = "container"
    image: str | None = None  # the client-supplied image reference
    registryUsername: str | None = None  # shown; the token is never returned
    port: int | None = None  # explicit container port, or None for Knative's default
