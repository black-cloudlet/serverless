"""Function request schemas."""

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

Runtime = Literal["python", "go", "javascript"]
Name = Annotated[str, AfterValidator(validate_name)]
Group = Annotated[str, AfterValidator(validate_group)]
Hostname = Annotated[str, AfterValidator(validate_hostname)]


class FunctionCreate(BaseModel):
    name: Name
    group: Group  # the SSO group to act as; caller must be a member
    gitRepo: str
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
    """Full replace of the mutable spec.

    Supplying any build input (gitRepo, branch, runtime) — or just a gitToken —
    triggers a rebuild from source; otherwise the existing image is kept and only
    config is updated.
    """

    group: Group  # the SSO group that owns the workload; caller must be a member
    # Rebuild inputs (all optional). gitRepo/branch/runtime default to the existing
    # values when omitted; gitToken is never stored, so it must be supplied to
    # rebuild (it can't be carried forward).
    gitRepo: str | None = None
    branch: str | None = None
    gitToken: str | None = None
    runtime: Runtime | None = None
    env: list[EnvVar] = Field(default_factory=list)
    files: list[FileMount] = Field(default_factory=list)
    scaling: Scaling = Field(default_factory=Scaling)
    size: WorkloadSize = "small"
    hostname: Hostname | None = None

    @model_validator(mode="after")
    def _rebuild_needs_token(self) -> "FunctionUpdate":
        if any(v is not None for v in (self.gitRepo, self.branch, self.runtime)) and (
            self.gitToken is None
        ):
            raise ValueError(
                "changing gitRepo/branch/runtime requires gitToken to rebuild"
            )
        return self

    @property
    def rebuild_requested(self) -> bool:
        return self.gitToken is not None


class FunctionResponse(WorkloadResponse):
    """A function, shaped like FunctionCreate (gitToken redacted) + live status.

    No image is exposed: the built image is an internal artifact — the client
    deals in source (gitRepo/branch), not images.
    """

    type: Literal["function", "container"] = "function"
    runtime: str | None = None
    gitRepo: str | None = None
    branch: str | None = None
