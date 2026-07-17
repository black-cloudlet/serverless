"""Container request schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from api.models.common import (
    EnvVar,
    FileMount,
    Hostname,
    Name,
    Scaling,
    WorkloadResponse,
    WorkloadSize,
)


class ContainerCreate(BaseModel):
    """Request body to deploy a pre-built container image.

    The owning group comes from the request path (``/api/v1/groups/{group}/...``),
    not the body.
    """

    name: Name
    image: str
    # Registry credentials are optional: omit both for a public image. When
    # supplied, both are required together (a username alone can't authenticate).
    registryUsername: str | None = None
    registryToken: str | None = None
    env: list[EnvVar] = Field(default_factory=list)
    files: list[FileMount] = Field(default_factory=list)
    scaling: Scaling = Field(default_factory=Scaling)
    size: WorkloadSize = "small"  # resource size; see services.ksvc
    sites: list[str] | None = None
    # Optional custom external host; defaults to {name}-{group}.{route_domain}.
    hostname: Hostname | None = None

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
    """Full replace of the mutable spec; image defaults to the current one."""

    image: str | None = None
    # Registry creds: username+token rotates, username-only keeps, neither removes
    # (public); a token needs a username. See docs §7.2 for the full semantics.
    registryUsername: str | None = None
    registryToken: str | None = None
    env: list[EnvVar] = Field(default_factory=list)
    files: list[FileMount] = Field(default_factory=list)
    scaling: Scaling = Field(default_factory=Scaling)
    size: WorkloadSize = "small"
    hostname: Hostname | None = None

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
