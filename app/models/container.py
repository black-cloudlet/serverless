"""CaaS request schemas."""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field

from app.models.common import EnvVar, FileMount, Scaling, validate_name

Name = Annotated[str, AfterValidator(validate_name)]


class ContainerCreate(BaseModel):
    name: Name
    image: str
    registryUsername: str
    registryToken: str
    env: list[EnvVar] = Field(default_factory=list)
    files: list[FileMount] = Field(default_factory=list)
    scaling: Scaling = Field(default_factory=Scaling)
    sites: list[str] | None = None


class ContainerUpdate(BaseModel):
    image: str | None = None
    env: list[EnvVar] | None = None
    files: list[FileMount] | None = None
    scaling: Scaling | None = None
