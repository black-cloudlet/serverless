"""Function request schemas."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field

from app.models.common import EnvVar, FileMount, Scaling, validate_hostname, validate_name

Runtime = Literal["python", "go", "javascript"]
Name = Annotated[str, AfterValidator(validate_name)]
Hostname = Annotated[str, AfterValidator(validate_hostname)]


class FunctionCreate(BaseModel):
    name: Name
    gitUrl: str
    branch: str = "main"
    gitToken: str
    runtime: Runtime
    env: list[EnvVar] = Field(default_factory=list)
    files: list[FileMount] = Field(default_factory=list)
    scaling: Scaling = Field(default_factory=Scaling)
    sites: list[str] | None = None
    # Optional custom external host; defaults to {name}-{group}.{route_domain}.
    hostname: Hostname | None = None


class FunctionUpdate(BaseModel):
    """Full replace of the mutable spec (config only; code changes go via create)."""

    env: list[EnvVar] = Field(default_factory=list)
    files: list[FileMount] = Field(default_factory=list)
    scaling: Scaling = Field(default_factory=Scaling)
    hostname: Hostname | None = None
