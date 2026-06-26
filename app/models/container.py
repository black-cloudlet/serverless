"""Container request schemas."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, model_validator

from app.models.common import (
    EnvVar,
    FileMount,
    Scaling,
    WorkloadResponse,
    WorkloadSize,
    validate_group,
    validate_hostname,
    validate_name,
)

Name = Annotated[str, AfterValidator(validate_name)]
Group = Annotated[str, AfterValidator(validate_group)]
Hostname = Annotated[str, AfterValidator(validate_hostname)]


class ContainerCreate(BaseModel):
    """Request body to deploy a pre-built container image."""

    name: Name
    group: Group  # the SSO group to act as; caller must be a member
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
        if (self.registryUsername is None) != (self.registryToken is None):
            raise ValueError(
                "registryUsername and registryToken must be provided together "
                "(or both omitted for a public image)"
            )
        return self


class ContainerUpdate(BaseModel):
    """Full replace of the mutable spec; image defaults to the current one."""

    group: Group  # the SSO group that owns the workload; caller must be a member
    image: str | None = None
    # Rotate/add registry creds; omit both to keep the existing pull secret. If
    # either is given, both are required together.
    registryUsername: str | None = None
    registryToken: str | None = None
    env: list[EnvVar] = Field(default_factory=list)
    files: list[FileMount] = Field(default_factory=list)
    scaling: Scaling = Field(default_factory=Scaling)
    size: WorkloadSize = "small"
    hostname: Hostname | None = None

    @model_validator(mode="after")
    def _registry_creds(self) -> "ContainerUpdate":
        if (self.registryUsername is None) != (self.registryToken is None):
            raise ValueError(
                "registryUsername and registryToken must be provided together"
            )
        return self


class ContainerResponse(WorkloadResponse):
    """A container, shaped like ContainerCreate (registry token redacted) + status."""

    type: Literal["function", "container"] = "container"
    image: str | None = None  # the client-supplied image reference
    registryUsername: str | None = None  # shown; the token is never returned
