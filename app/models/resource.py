"""Schemas for API-managed workload secrets & configs (docs §7.3)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field

from app.models.common import ZoneStatus, validate_name

Name = Annotated[str, AfterValidator(validate_name)]


class ResourceCreate(BaseModel):
    name: Name
    data: dict[str, str] = Field(..., min_length=1)


class ResourceResponse(BaseModel):
    name: str
    type: Literal["secret", "config"]
    keys: list[str]
    overallStatus: str
    zones: list[ZoneStatus]
    # Secret values are redacted by default; configs return data.
    data: dict[str, str] | None = None
