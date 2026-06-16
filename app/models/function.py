"""FaaS request schemas."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field

from app.models.common import EnvVar, FileMount, Scaling, validate_name

Runtime = Literal["python", "go", "javascript"]
Name = Annotated[str, AfterValidator(validate_name)]


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


class FunctionUpdate(BaseModel):
    env: list[EnvVar] | None = None
    files: list[FileMount] | None = None
    scaling: Scaling | None = None
    rebuild: bool = False
