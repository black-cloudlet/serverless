"""Shared schemas and label constants used across the API."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal, get_args

from pydantic import BaseModel, Field, model_validator

# Ownership label keys are shared platform identity (common.labels); re-exported
# here for the api models/services that read and stamp them.
from common.labels import (  # noqa: F401
    LABEL_GROUP,
    LABEL_MANAGED_BY,
    LABEL_OFFERING,
    LABEL_OWNER,
    LABEL_WORKLOAD,
    MANAGED_BY_VALUE,
)

# In `common` because they bound what can be written to a cluster, and the
# builder applies them off the HTTP path. Re-exported for one import site.
from common.names import (  # noqa: F401
    DNS1123,
    HOSTNAME,
    Branch,
    GitUrl,
    Group,
    Hostname,
    ImageRef,
    Name,
    SourcePath,
    normalize_group,
    validate_branch,
    validate_git_url,
    validate_group,
    validate_hostname,
    validate_image_ref,
    validate_name,
    validate_source_path,
)

ANNOTATION_HOST = "serverless.platform/host"
ANNOTATION_SIZE = "serverless.platform/size"
ANNOTATION_RUNTIME = "serverless.platform/runtime"
ANNOTATION_GIT_URL = "serverless.platform/git-url"
ANNOTATION_GIT_BRANCH = "serverless.platform/git-branch"
ANNOTATION_GIT_PATH = "serverless.platform/git-path"
# Names of the injected CA-trust env vars, so read-back can hide them: they
# are platform defaults, not part of the user's spec.
ANNOTATION_INJECTED_ENV = "serverless.platform/injected-env"

# The injected CA-bundle volume/mount name. An internal handle, filtered out
# of read-back as it is not part of the user's spec.
CA_BUNDLE_VOLUME = "ca-bundle"

# Knative autoscaler metrics. concurrency/rps use the default KPA (scale-to-zero
# capable); cpu/memory use the HPA autoscaler class (no scale-to-zero).
ScalingMetric = Literal["concurrency", "rps", "cpu", "memory"]
_KPA_METRICS = {"concurrency", "rps"}
# Per-metric target defaults and bounds (the single source both the validator and
# the /info capabilities projection read, so they can't drift).
_TARGET_MIN = 1
_KPA_TARGET_DEFAULT = 100  # concurrency/rps: absolute request count per replica
_HPA_TARGET_DEFAULT = 70  # cpu/memory: utilization percentage
_HPA_TARGET_MAX = 100  # a utilization percentage can't exceed 100
_METRIC_UNITS = {
    "concurrency": "concurrentRequests",
    "rps": "requestsPerSecond",
    "cpu": "percent",
    "memory": "percent",
}

# T-shirt sizing (see services.ksvc): memory is request==limit, CPU is
# request-only so the workload is never throttled.
WorkloadSize = Literal["small", "medium", "large"]

# The rollup a client polls on. A Literal, not a comment, so it is enforced on
# every response and /info can advertise it instead of a portal hardcoding it.
WorkloadStatus = Literal["Pending", "Building", "Deploying", "Ready", "Degraded", "Terminating"]
# Per-site values that reach a response. SiteStatus is also the return type of the
# internal host/absence probes (Available, Absent, ...), so the field itself stays
# a plain str and only the client-facing set is published.
SITE_STATUSES = ("Ready", "Deploying", "Failed", "Terminating", "Timeout")

_DURATION = re.compile(r"^(\d+)(s|m|h)$")
_DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600}
_SCALE_DOWN_DELAY_MAX_SECONDS = 3600  # Knative maximum
_SCALE_DOWN_DELAY_MAX = "1h"


class EnvVar(BaseModel):
    """An environment variable: ``name`` + ``value``.

    With ``secret: true`` the API stores the value in a Kubernetes Secret and the
    container reads it via a secretKeyRef (the value is never inline on the KSVC).

    ``value`` is optional only for a secret var: a secret entry with ``value:
    null`` means "keep the stored value" on update (the redacted read - ``secret:
    true, value: null`` - can be sent straight back). A non-secret var always
    needs a value, and a brand-new secret needs one too (there is nothing to keep).
    """

    name: str
    value: str | None = None
    secret: bool = False

    @model_validator(mode="after")
    def _non_secret_needs_value(self) -> "EnvVar":
        """A non-secret env var must carry a value (only secrets may keep)."""
        if not self.secret and self.value is None:
            raise ValueError(f"env var '{self.name}' requires a value")
        return self


class FileMount(BaseModel):
    """An inline file to load into the workload at ``mountPath``.

    The API stores the content in the workload's shared ConfigMap (or Secret when
    ``secret: true``) - one ConfigMap and one Secret per workload - and mounts
    each file at its ``mountPath`` via ``subPath``.

    A secret file may omit both content fields, meaning "keep the stored content"
    on update (the redacted read - ``secret: true, content: null`` - can be sent
    straight back). A non-secret file always needs exactly one content field, and
    supplying both is always rejected.
    """

    mountPath: str
    content: str | None = None
    contentBase64: str | None = None
    secret: bool = False
    readOnly: bool = True

    @property
    def keep(self) -> bool:
        """Whether this (secret) file keeps its stored content (no content given)."""
        return self.content is None and self.contentBase64 is None

    @model_validator(mode="after")
    def _check(self) -> "FileMount":
        """Validate the content fields (exactly one, or none only for a secret keep)."""
        if self.content is not None and self.contentBase64 is not None:
            raise ValueError("file accepts at most one of 'content' or 'contentBase64'")
        if self.keep and not self.secret:
            raise ValueError("file requires exactly one of 'content' or 'contentBase64'")
        return self


class Scaling(BaseModel):
    """Autoscaling settings: replica bounds, the metric, and its target.

    Attributes:
        minScale: Minimum replicas (0 allows scale-to-zero for KPA metrics).
        maxScale: Maximum replicas.
        metric: The signal the autoscaler scales on (concurrency/rps/cpu/memory).
        target: Target value for the metric; None uses a metric-aware default.
        scaleDownDelay: How long the autoscaler waits before scaling a revision
            down (e.g. "30s", "5m", "1h"); None leaves the Knative default.
            Smooths bursty traffic by avoiding rapid scale-down/up churn.
    """

    minScale: int = Field(0, ge=0)
    maxScale: int = Field(3, ge=1)
    metric: ScalingMetric = "concurrency"
    target: int | None = Field(None, ge=_TARGET_MIN)
    scaleDownDelay: str | None = None

    @property
    def effective_target(self) -> int:
        """The target value to apply, defaulting by metric when unset.

        Returns:
            ``target`` if set, else the KPA/HPA default for the metric.
        """
        if self.target is not None:
            return self.target
        return _KPA_TARGET_DEFAULT if self.metric in _KPA_METRICS else _HPA_TARGET_DEFAULT

    @classmethod
    def capabilities(cls) -> "ScalingCapabilities":
        """Project the per-metric scaling rules for the public /info endpoint.

        Derived from the same constants the validator enforces (``_KPA_METRICS``,
        the target defaults/bounds, the duration cap), so the advertised
        capabilities can't drift from what a create request will accept.
        """
        metrics = []
        for name in get_args(ScalingMetric):
            is_kpa = name in _KPA_METRICS
            metrics.append(
                MetricCapability(
                    name=name,
                    minScaleFloor=0 if is_kpa else 1,
                    target=MetricTarget(
                        default=_KPA_TARGET_DEFAULT if is_kpa else _HPA_TARGET_DEFAULT,
                        min=_TARGET_MIN,
                        max=None if is_kpa else _HPA_TARGET_MAX,
                        unit=_METRIC_UNITS[name],
                    ),
                )
            )
        return ScalingCapabilities(
            defaultMetric=cls.model_fields["metric"].default,
            metrics=metrics,
            scaleDownDelay=ScaleDownDelayCapability(
                format="duration",
                min="0s",
                max=_SCALE_DOWN_DELAY_MAX,
                default=cls.model_fields["scaleDownDelay"].default,
            ),
        )

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
        if (
            self.metric not in _KPA_METRICS
            and self.target is not None
            and self.target > _HPA_TARGET_MAX
        ):
            raise ValueError(
                f"metric '{self.metric}' target is a utilization percentage; "
                f"it must be between {_TARGET_MIN} and {_HPA_TARGET_MAX}"
            )
        if self.scaleDownDelay is not None:
            match = _DURATION.fullmatch(self.scaleDownDelay)
            if not match:
                raise ValueError("scaleDownDelay must be a duration like '30s', '5m', or '1h'")
            seconds = int(match.group(1)) * _DURATION_SECONDS[match.group(2)]
            if seconds > _SCALE_DOWN_DELAY_MAX_SECONDS:
                raise ValueError(
                    f"scaleDownDelay must be at most {_SCALE_DOWN_DELAY_MAX} (Knative maximum)"
                )
        return self


class MetricTarget(BaseModel):
    """The target bounds for one autoscaling metric (public capability).

    Attributes:
        default: The target applied when the client omits one.
        min: The smallest accepted target.
        max: The largest accepted target, or None when unbounded (KPA metrics).
        unit: What the target counts (e.g. "percent", "concurrentRequests").
    """

    default: int
    min: int
    max: int | None
    unit: str


class MetricCapability(BaseModel):
    """One autoscaling metric's client-facing rules.

    Attributes:
        name: The metric name (concurrency/rps/cpu/memory).
        minScaleFloor: The smallest allowed ``minScale`` (0 means scale-to-zero
            is permitted; 1 means it isn't).
        target: The metric's target bounds and unit.
    """

    name: str
    minScaleFloor: int
    target: MetricTarget


class ScaleDownDelayCapability(BaseModel):
    """The accepted shape and bounds of ``scaleDownDelay``."""

    format: str
    min: str
    max: str
    default: str | None = None


class ScalingCapabilities(BaseModel):
    """The scaling options a client may choose from, for dynamic UI rendering."""

    defaultMetric: str
    metrics: list[MetricCapability]
    scaleDownDelay: ScaleDownDelayCapability


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
    overallStatus: WorkloadStatus
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

    Secret-backed values are never returned - ``secret: true`` with
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


class BuildStatusView(BaseModel):
    """A function's image build state, read from the local site's kpack Image.

    State and reason only. The built image stays internal - a function's client
    deals in source, not images - so it is on :class:`common.contract.BuildStatus`,
    which the build service reads, and never on the response.

    Attributes:
        state: Building / Ready / Failed / Unknown.
        message: Why the build failed, when it did.
    """

    state: str
    message: str | None = None


class WorkloadResponse(WorkloadBase):
    """Full single-workload view: identity, live per-site status, and config.

    Identity, live per-site status, and the desired-state config common to both
    offerings (secrets redacted). FunctionResponse and ContainerResponse subclass
    this, so a response mirrors the create body of its offering.
    """

    sites: list[SiteStatus] = []
    statusUrl: str | None = None
    # desired-state config common to both offerings (secret values redacted)
    scaling: Scaling | None = None
    env: list[EnvVarView] = []
    files: list[FileView] = []


class PodLogs(BaseModel):
    """The current log snapshot of one workload pod's container.

    Attributes:
        pod: The pod name.
        container: The container the log was read from.
        revision: The Knative revision the pod belongs to, if labelled.
        logs: The log text as the node currently holds it (timestamped).
    """

    pod: str
    container: str
    revision: str | None = None
    logs: str


class LogsResponse(BaseModel):
    """A workload's pod logs from the local site (a point-in-time snapshot).

    Logs are node-local and ephemeral: only the running pods on the current site
    are read, and their history is bounded by the node's log rotation. Empty
    ``pods`` means the workload is deployed here but scaled to zero.
    """

    name: str
    group: str
    type: Literal["function", "container"]
    site: str
    pods: list[PodLogs] = []


class WorkloadSpec(BaseModel):
    """The desired-state spec read back from a deployed workload, secrets redacted.

    Redacts all secret material: secret env values, secret file contents, the
    registry token and the git token.
    """

    scaling: "Scaling | None" = None
    env: list[EnvVarView] = []
    files: list[FileView] = []
    # Explicit container port, or None when the workload uses Knative's default.
    port: int | None = None
    # Container source: registry username is shown (like a secret's name); the
    # token is never returned. None when the image is public (no pull secret).
    registryUsername: str | None = None
    # Function source: what the build was run from. The git token is never stored.
    gitRepo: str | None = None
    branch: str | None = None
    # Sub-directory inside the repository; None (or absent) means the root.
    path: str | None = None
