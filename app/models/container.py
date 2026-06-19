"""Container request schemas."""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field

from app.models.common import (
    EnvVar,
    FileMount,
    Scaling,
    WorkloadSize,
    validate_group,
    validate_hostname,
    validate_name,
)

Name = Annotated[str, AfterValidator(validate_name)]
Group = Annotated[str, AfterValidator(validate_group)]
Hostname = Annotated[str, AfterValidator(validate_hostname)]


class ContainerCreate(BaseModel):
    name: Name
    group: Group  # the SSO group to act as; caller must be a member
    image: str
    registryUsername: str
    registryToken: str
    env: list[EnvVar] = Field(default_factory=list)
    files: list[FileMount] = Field(default_factory=list)
    scaling: Scaling = Field(default_factory=Scaling)
    size: WorkloadSize = "small"  # resource size; see services.ksvc
    sites: list[str] | None = None
    # Optional custom external host; defaults to {name}-{group}.{route_domain}.
    hostname: Hostname | None = None


class ContainerUpdate(BaseModel):
    """Full replace of the mutable spec; image defaults to the current one."""

    group: Group  # the SSO group that owns the workload; caller must be a member
    image: str | None = None
    env: list[EnvVar] = Field(default_factory=list)
    files: list[FileMount] = Field(default_factory=list)
    scaling: Scaling = Field(default_factory=Scaling)
    size: WorkloadSize = "small"
    hostname: Hostname | None = None
