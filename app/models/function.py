"""Function request schemas."""

from __future__ import annotations

from typing import Annotated, Literal

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

Runtime = Literal["python", "go", "javascript"]
Name = Annotated[str, AfterValidator(validate_name)]
Group = Annotated[str, AfterValidator(validate_group)]
Hostname = Annotated[str, AfterValidator(validate_hostname)]


class FunctionCreate(BaseModel):
    name: Name
    group: Group  # the SSO group to act as; caller must be a member
    gitUrl: str
    branch: str = "main"
    gitToken: str
    runtime: Runtime
    env: list[EnvVar] = Field(default_factory=list)
    files: list[FileMount] = Field(default_factory=list)
    scaling: Scaling = Field(default_factory=Scaling)
    size: WorkloadSize = "small"  # resource size; see services.ksvc
    sites: list[str] | None = None
    # Optional custom external host; defaults to {name}-{group}.{route_domain}.
    hostname: Hostname | None = None


class FunctionUpdate(BaseModel):
    """Full replace of the mutable spec (config only; code changes go via create)."""

    group: Group  # the SSO group that owns the workload; caller must be a member
    env: list[EnvVar] = Field(default_factory=list)
    files: list[FileMount] = Field(default_factory=list)
    scaling: Scaling = Field(default_factory=Scaling)
    size: WorkloadSize = "small"
    hostname: Hostname | None = None
