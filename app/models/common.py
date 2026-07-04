"""Shared schemas and label constants used across the API."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, model_validator

LABEL_GROUP = "serverless.platform/group"
LABEL_MANAGED_BY = "serverless.platform/managed-by"
LABEL_OWNER = "serverless.platform/owner"
LABEL_OFFERING = "serverless.platform/offering"
LABEL_WORKLOAD = "serverless.platform/workload"
MANAGED_BY_VALUE = "serverless-api"

ANNOTATION_HOST = "serverless.platform/host"
ANNOTATION_SIZE = "serverless.platform/size"
ANNOTATION_RUNTIME = "serverless.platform/runtime"
ANNOTATION_GIT_URL = "serverless.platform/git-url"
ANNOTATION_GIT_BRANCH = "serverless.platform/git-branch"

# Name of the platform-injected CA-bundle volume/mount on every pod (matches the
# default ConfigMap name). An internal pod-spec handle — owned by the KSVC
# builder; read-back filters it out as it isn't part of the user's spec.
CA_BUNDLE_VOLUME = "ca-bundle"

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
# Leading "ggd-<1-4 digits>-" prefix some OIDC groups carry (e.g.
# "ggd-1234-platforms" is the group "platforms").
_GGD_PREFIX = re.compile(r"^ggd-\d{1,4}-")


def normalize_group(group: str) -> str:
    """Normalize a group name to its bare form.

    Strips the Keycloak path prefix ("/") and a leading ``ggd-<1-4 digits>-``
    prefix, so e.g. "/ggd-1234-platforms" and "platforms" name the same group.
    Applied both to groups from the OIDC token and to a request-supplied group.

    Args:
        group: The raw group name.

    Returns:
        The normalized group name.
    """
    return _GGD_PREFIX.sub("", group.lstrip("/"))


def validate_name(name: str) -> str:
    """Validate a workload name as a DNS-1123 label.

    Args:
        name: The candidate workload name.

    Returns:
        The name unchanged.

    Raises:
        ValueError: If it isn't a DNS-1123 label of at most 63 characters.
    """
    if not DNS1123.match(name) or len(name) > 63:
        raise ValueError(
            "name must be a DNS-1123 label (lowercase alphanumeric and '-', <=63 chars)"
        )
    return name


def validate_group(group: str) -> str:
    """Normalize and validate a group name as a DNS-1123 label.

    Args:
        group: The candidate group name (a ``ggd-<digits>-`` prefix is stripped).

    Returns:
        The normalized group name.

    Raises:
        ValueError: If it isn't a DNS-1123 label of at most 63 characters.
    """
    group = normalize_group(group)
    if not DNS1123.match(group) or len(group) > 63:
        raise ValueError(
            "group must be a DNS-1123 label (lowercase alphanumeric and '-', <=63 chars)"
        )
    return group


def validate_hostname(host: str) -> str:
    """Validate a custom hostname as a DNS-1123 label or a lowercase FQDN.

    Either a single DNS-1123 label (the platform base domain is appended by the
    API) or a full lowercase FQDN. That the FQDN sits under the platform base
    domain is enforced in the service layer, where the base domain is known.

    Args:
        host: The candidate hostname.

    Returns:
        The host unchanged.

    Raises:
        ValueError: If it is neither a DNS-1123 label nor a valid lowercase FQDN.
    """
    if (DNS1123.match(host) and len(host) <= 63) or HOSTNAME.match(host):
        return host
    raise ValueError("hostname must be a DNS-1123 label or a valid lowercase FQDN")


# Validated string types shared by request models and query params. The group
# validator also NORMALIZES ("/ggd-1234-team" -> "team"), so every group entering
# the app is already in bare, canonical form at the edge — nothing downstream
# re-normalizes.
Name = Annotated[str, AfterValidator(validate_name)]
Group = Annotated[str, AfterValidator(validate_group)]
Hostname = Annotated[str, AfterValidator(validate_hostname)]


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
        """Require exactly one of ``content`` or ``contentBase64``."""
        if (self.content is None) == (self.contentBase64 is None):
            raise ValueError("file requires exactly one of 'content' or 'contentBase64'")
        return self


class Scaling(BaseModel):
    """Autoscaling settings: replica bounds, the metric, and its target.

    Attributes:
        minScale: Minimum replicas (0 allows scale-to-zero for KPA metrics).
        maxScale: Maximum replicas.
        metric: The signal the autoscaler scales on (concurrency/rps/cpu/memory).
        target: Target value for the metric; None uses a metric-aware default.
    """

    minScale: int = Field(0, ge=0)
    maxScale: int = Field(3, ge=1)
    metric: ScalingMetric = "concurrency"
    target: int | None = Field(None, ge=1)

    @property
    def effective_target(self) -> int:
        """The target value to apply, defaulting by metric when unset.

        Returns:
            ``target`` if set, else 100 for concurrency/rps or 70 (%) for
            cpu/memory.
        """
        if self.target is not None:
            return self.target
        return 100 if self.metric in _KPA_METRICS else 70

    @property
    def autoscaler_class(self) -> str | None:
        """The Knative autoscaler class annotation, or None for the default (KPA)."""
        return None if self.metric in _KPA_METRICS else "hpa.autoscaling.knative.dev"

    @model_validator(mode="after")
    def _bounds(self) -> "Scaling":
        """Validate replica bounds and HPA-metric constraints."""
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
    """The deploy/health state of a workload at a single site.

    Attributes:
        site: The site name.
        status: Per-site status (Ready/Deploying/Failed/Terminating/Timeout/...).
        revision: The Knative revision the site is serving, if known.
        error: The failure message when the site errored, else None.
    """

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


class WorkloadBase(BaseModel):
    """Identity fields common to every workload view (list item and full GET)."""

    name: str
    group: str  # the owning SSO group
    type: Literal["function", "container"]
    hostname: str  # external host (no scheme), e.g. {name}-{group}.{route_domain}
    overallStatus: str  # Pending | Ready | Deploying | Degraded | Terminating
    size: str | None = None  # resource t-shirt size (uniform across sites)
    # workload creation time (metadata.creationTimestamp), in Israel local time
    createdAt: datetime | None = None


class WorkloadSummary(WorkloadBase):
    """Lightweight list item: general info only, no per-site live usage.

    Use the single-workload GET for replicas/usage.
    """

    sites: list[str] = []  # site names where the workload is deployed


class EnvVarView(BaseModel):
    """An env var as read back from a deployed workload.

    Secret-backed values are never returned — ``secret: true`` with
    ``value: null`` signals one is set.
    """

    name: str
    value: str | None = None
    secret: bool = False


class FileView(BaseModel):
    """A mounted file as read back from a deployed workload.

    ``content`` is returned only for non-secret (ConfigMap-backed) files; it is
    always null for secret files.
    """

    mountPath: str
    readOnly: bool = True
    secret: bool = False
    content: str | None = None


class WorkloadResponse(WorkloadBase):
    """Full single-workload view: identity, live per-site status, and config.

    Identity (WorkloadBase) plus live per-site status plus the desired-state
    config common to both offerings (secrets redacted). Per-offering responses
    subclass this — see FunctionResponse (in models.function) / ContainerResponse
    (in models.container) — so the response mirrors the create body of that
    offering.
    """

    sites: list[SiteStatus] = []
    statusUrl: str | None = None
    # desired-state config common to both offerings (secret values redacted)
    scaling: Scaling | None = None
    env: list[EnvVarView] = []
    files: list[FileView] = []


class WorkloadSpec(BaseModel):
    """The desired-state spec read back from a deployed workload, secrets redacted.

    Redacts all secret material: secret env values, secret file contents, the
    registry token and the git token.
    """

    scaling: "Scaling | None" = None
    env: list[EnvVarView] = []
    files: list[FileView] = []
    # Container source: registry username is shown (like a secret's name); the
    # token is never returned. None when the image is public (no pull secret).
    registryUsername: str | None = None
    # Function source: what the build was run from. The git token is never stored.
    gitRepo: str | None = None
    branch: str | None = None
