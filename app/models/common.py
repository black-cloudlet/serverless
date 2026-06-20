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
# Annotation recording the chosen t-shirt size (so reads can report it).
ANNOTATION_SIZE = "serverless.platform/size"
# Function build inputs, stamped so reads can report what was submitted. The git
# token is deliberately NOT among these — it is never persisted (docs §7.2).
ANNOTATION_RUNTIME = "serverless.platform/runtime"
ANNOTATION_GIT_URL = "serverless.platform/git-url"
ANNOTATION_GIT_BRANCH = "serverless.platform/git-branch"

# Knative autoscaler metrics. concurrency/rps use the default KPA (scale-to-zero
# capable); cpu/memory use the HPA autoscaler class (no scale-to-zero).
ScalingMetric = Literal["concurrency", "rps", "cpu", "memory"]
_KPA_METRICS = {"concurrency", "rps"}

# Workload resource size (t-shirt sizing). Maps to container resources in the
# KSVC (see services.ksvc): memory is request==limit (hard cap), CPU is
# request-only (no limit -> no throttling).
WorkloadSize = Literal["small", "medium", "large"]

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
    maxScale: int = Field(3, ge=1)
    # The signal the Knative autoscaler scales on, and the target value for it:
    #   concurrency -> in-flight requests per replica (default KPA)
    #   rps         -> requests per second per replica (KPA)
    #   cpu/memory  -> % CPU/memory utilization (HPA class; cannot scale to zero)
    metric: ScalingMetric = "concurrency"
    # Omitted -> a metric-aware default is used (see effective_target): 100 for
    # concurrency/rps, 70 (%) for cpu/memory so we scale before saturation.
    target: int | None = Field(None, ge=1)

    @property
    def effective_target(self) -> int:
        if self.target is not None:
            return self.target
        return 100 if self.metric in _KPA_METRICS else 70

    @property
    def autoscaler_class(self) -> str | None:
        """The Knative autoscaler class annotation, or None for the default (KPA)."""
        return None if self.metric in _KPA_METRICS else "hpa.autoscaling.knative.dev"

    @model_validator(mode="after")
    def _bounds(self) -> "Scaling":
        if self.maxScale < self.minScale:
            raise ValueError("maxScale must be >= minScale")
        if self.metric not in _KPA_METRICS and self.minScale < 1:
            raise ValueError(
                f"metric '{self.metric}' uses the HPA autoscaler, which cannot "
                "scale to zero; set minScale >= 1"
            )
        # cpu/memory targets are a utilization percentage; >100 makes no sense.
        if self.metric not in _KPA_METRICS and self.target is not None and self.target > 100:
            raise ValueError(
                f"metric '{self.metric}' target is a utilization percentage; "
                "it must be between 1 and 100"
            )
        return self


class SiteStatus(BaseModel):
    site: str
    status: str
    revision: str | None = None
    error: str | None = None
    replicas: int | None = None  # running pods at this site (None if unknown)
    usage: "ResourceUsage | None" = None  # live cpu/memory summed over those pods


class ResourceUsage(BaseModel):
    """Live resource consumption summed over a workload's running pods."""

    cpu: str | None = None  # e.g. "120m"
    memory: str | None = None  # e.g. "180Mi"


class WorkloadSummary(BaseModel):
    """Lightweight list item: general info only, no per-site live usage. Use the
    single-workload GET for replicas/usage."""

    name: str
    type: Literal["function", "container"]
    url: str
    overallStatus: str  # Ready | Deploying | Degraded
    size: str | None = None
    sites: list[str] = []  # site names where the workload is deployed


class EnvVarView(BaseModel):
    """An env var as read back from a deployed workload. Secret-backed values are
    never returned — `secret: true` with `value: null` signals one is set."""

    name: str
    value: str | None = None
    secret: bool = False


class FileView(BaseModel):
    """A mounted file as read back. `content` is returned only for non-secret
    (ConfigMap-backed) files; it is always null for secret files."""

    mountPath: str
    readOnly: bool = True
    secret: bool = False
    content: str | None = None


class WorkloadResponse(BaseModel):
    """Common fields shared by both offerings: identity + live status + the
    desired-state config that's common to functions and containers (secrets
    redacted). Per-offering responses subclass this — see FunctionResponse (in
    models.function) / ContainerResponse (in models.container) — so the response
    mirrors the create body of that offering."""

    name: str
    type: Literal["function", "container"]
    url: str
    # Pending (accepted, deploying in background) | Ready | Deploying | Degraded
    overallStatus: str
    size: str | None = None  # resource t-shirt size (uniform across sites)
    sites: list[SiteStatus] = []
    statusUrl: str | None = None
    createdAt: datetime | None = None
    # desired-state config common to both offerings (secret values redacted)
    scaling: Scaling | None = None
    env: list[EnvVarView] = []
    files: list[FileView] = []


class WorkloadSpec(BaseModel):
    """The desired-state spec read back from a deployed workload, with all secret
    material redacted (secret env values, secret file contents, the registry token
    and the git token)."""

    scaling: "Scaling | None" = None
    env: list[EnvVarView] = []
    files: list[FileView] = []
    # Container source: registry username is shown (like a secret's name); the
    # token is never returned. None when the image is public (no pull secret).
    registryUsername: str | None = None
    # Function source: what the build was run from. The git token is never stored.
    gitRepo: str | None = None
    branch: str | None = None

