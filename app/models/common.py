"""Shared schemas and label constants used across the API."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# Ownership / management labels stamped on every resource (docs §6.2).
LABEL_GROUP = "serverless.platform/group"
LABEL_MANAGED_BY = "serverless.platform/managed-by"
LABEL_OWNER = "serverless.platform/owner"
LABEL_OFFERING = "serverless.platform/offering"
MANAGED_BY_VALUE = "serverless-api"

DNS1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def validate_name(name: str) -> str:
    if not DNS1123.match(name) or len(name) > 63:
        raise ValueError(
            "name must be a DNS-1123 label (lowercase alphanumeric and '-', <=63 chars)"
        )
    return name


class ValueFrom(BaseModel):
    secret: str | None = None
    configmap: str | None = None
    key: str


class EnvVar(BaseModel):
    name: str
    value: str | None = None
    valueFrom: ValueFrom | None = None

    @model_validator(mode="after")
    def _one_of(self) -> "EnvVar":
        if (self.value is None) == (self.valueFrom is None):
            raise ValueError("env var requires exactly one of 'value' or 'valueFrom'")
        return self


class FileMount(BaseModel):
    """A file to load into the workload at ``mountPath``.

    Either upload inline content (``content`` / ``contentBase64``) — the API
    creates the backing ConfigMap/Secret — or reference an existing API-managed
    resource by ``source``.
    """

    mountPath: str
    content: str | None = None
    contentBase64: str | None = None
    secret: bool = False
    source: str | None = None
    type: Literal["configmap", "secret"] | None = None

    @model_validator(mode="after")
    def _check(self) -> "FileMount":
        inline = self.content is not None or self.contentBase64 is not None
        if inline == bool(self.source):
            raise ValueError(
                "file requires exactly one of inline content or 'source'"
            )
        if self.content is not None and self.contentBase64 is not None:
            raise ValueError("provide only one of 'content' or 'contentBase64'")
        return self


class Scaling(BaseModel):
    minScale: int = Field(0, ge=0)
    maxScale: int = Field(10, ge=1)
    targetConcurrency: int = Field(100, ge=1)
    containerConcurrency: int = Field(0, ge=0)

    @model_validator(mode="after")
    def _bounds(self) -> "Scaling":
        if self.maxScale < self.minScale:
            raise ValueError("maxScale must be >= minScale")
        return self


class SiteStatus(BaseModel):
    site: str
    status: str
    revision: str | None = None
    error: str | None = None


class WorkloadResponse(BaseModel):
    name: str
    type: Literal["function", "container"]
    url: str
    overallStatus: str
    sites: list[SiteStatus]
    runtime: str | None = None
    image: str | None = None
    imageDigest: str | None = None
    createdAt: datetime | None = None
