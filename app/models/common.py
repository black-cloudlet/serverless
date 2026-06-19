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
LABEL_WORKLOAD = "serverless.platform/workload"
MANAGED_BY_VALUE = "serverless-api"

# Annotation recording the external host chosen for a workload (so reads can
# report the URL without recomputing/guessing it).
ANNOTATION_HOST = "serverless.platform/host"

DNS1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
# RFC-1123 hostname (FQDN): lowercase labels separated by dots, <=253 chars.
HOSTNAME = re.compile(
    r"^(?=.{1,253}$)[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)+$"
)


def validate_name(name: str) -> str:
    if not DNS1123.match(name) or len(name) > 63:
        raise ValueError(
            "name must be a DNS-1123 label (lowercase alphanumeric and '-', <=63 chars)"
        )
    return name


def validate_group(group: str) -> str:
    if not DNS1123.match(group) or len(group) > 63:
        raise ValueError(
            "group must be a DNS-1123 label (lowercase alphanumeric and '-', <=63 chars)"
        )
    return group


def validate_hostname(host: str) -> str:
    # Either a single DNS-1123 label (the platform base domain is appended by the
    # API) or a full lowercase FQDN. That the FQDN sits under the platform base
    # domain is enforced in the service layer, where the base domain is known.
    if (DNS1123.match(host) and len(host) <= 63) or HOSTNAME.match(host):
        return host
    raise ValueError("hostname must be a DNS-1123 label or a valid lowercase FQDN")


class EnvVar(BaseModel):
    """An environment variable: ``name`` + ``value``.

    With ``secret: true`` the API stores the value in a Kubernetes Secret and the
    container reads it via a secretKeyRef (the value is never inline on the KSVC).
    """

    name: str
    value: str
    secret: bool = False


class FileMount(BaseModel):
    """An inline file to load into the workload at ``mountPath``.

    The API stores the content in the workload's shared ConfigMap (or Secret when
    ``secret: true``) — one ConfigMap and one Secret per workload — and mounts
    each file at its ``mountPath`` via ``subPath``.
    """

    mountPath: str
    content: str | None = None
    contentBase64: str | None = None
    secret: bool = False
    readOnly: bool = True

    @model_validator(mode="after")
    def _check(self) -> "FileMount":
        if (self.content is None) == (self.contentBase64 is None):
            raise ValueError(
                "file requires exactly one of 'content' or 'contentBase64'"
            )
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
    # Pending (accepted, deploying in background) | Ready | Deploying | Degraded
    overallStatus: str
    sites: list[SiteStatus] = []
    statusUrl: str | None = None
    runtime: str | None = None
    image: str | None = None
    imageDigest: str | None = None
    createdAt: datetime | None = None
