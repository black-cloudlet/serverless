"""Shared schemas and label constants used across the API.

The response models both offerings return (the per-region rows, the status
rollup, the pod roster and log shapes), the annotation and label keys the
manifests stamp, and the ``Annotated`` path and query types the routers
validate with. The name and image-reference validators are re-exported from
:mod:`common.names`, so request models and the builder apply the same rules.
"""

from __future__ import annotations

import base64
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

# Name/reference types and validators bound what can be written to a cluster, so
# they live in `common` and are applied off the HTTP path by the builder too.
# Re-exported here for one import region.
from common.names import (  # noqa: F401
    DNS1123,
    HOSTNAME,
    EnvVarName,
    GitUrl,
    Group,
    Hostname,
    ImageRef,
    MountPath,
    Name,
    PodName,
    Revision,
    SourcePath,
    normalize_group,
    validate_env_var_name,
    validate_git_url,
    validate_group,
    validate_hostname,
    validate_image_ref,
    validate_mount_path,
    validate_name,
    validate_pod_name,
    validate_revision,
    validate_source_path,
)

ANNOTATION_HOST = "serverless.platform/host"
ANNOTATION_SIZE = "serverless.platform/size"
ANNOTATION_RUNTIME = "serverless.platform/runtime"
# The version the caller asked for; absent when they took the platform default.
ANNOTATION_RUNTIME_VERSION = "serverless.platform/runtime-version"
ANNOTATION_GIT_URL = "serverless.platform/git-url"
ANNOTATION_GIT_REVISION = "serverless.platform/git-revision"
# The exact commit a git push delivered, pinned by the webhook and cleared by
# every human write (POST .../build and PUT). Absent means the build follows
# ANNOTATION_GIT_REVISION - its head, when that names a branch.
ANNOTATION_GIT_COMMIT = "serverless.platform/git-commit"
ANNOTATION_GIT_PATH = "serverless.platform/git-path"
# Names of the injected CA-trust env vars, so read-back can hide them: they
# are platform defaults, not part of the user's spec.
ANNOTATION_INJECTED_ENV = "serverless.platform/injected-env"
# Set by POST .../pull, on the template (what Knative diffs, so it cuts a
# revision) and the metadata (the copy an update reads back).
ANNOTATION_PULL_STAMP = "serverless.platform/pull-stamp"

# The injected CA-bundle volume/mount name. An internal handle, filtered out
# of read-back as it is not part of the user's spec.
CA_BUNDLE_VOLUME = "ca-bundle"

# Container port bounds (a TCP port). The single source the field validators on
# both offerings and the /info capabilities projection read.
PORT_MIN = 1
PORT_MAX = 65535
# Knative's own default, and what it injects as $PORT when a container declares
# no port. Applied as the field default for both offerings, so a workload's port
# is stamped on the KSVC and read back rather than left implicit.
DEFAULT_PORT = 8080

# Knative autoscaler metrics. concurrency/rps use the default KPA (scale-to-zero
# capable); cpu/memory use the HPA autoscaler class (no scale-to-zero).
ScalingMetric = Literal["concurrency", "rps", "cpu", "memory"]
_KPA_METRICS = {"concurrency", "rps"}
# Per-metric target defaults and bounds - the single source both the validator
# and the /info capabilities projection read.
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

# The rollup a client polls on, enforced on every response and published on
# /info as statuses.workload. A closed set, like a Kubernetes phase: a cause is
# never promoted into it, it goes on `reason`.
WorkloadStatus = Literal["Pending", "Building", "Deploying", "Ready", "Failed", "Terminating"]
# Per-region values that reach a response. RegionStatus is also the return type of the
# internal host/absence probes (Available, Absent, ...), so the field itself stays
# a plain str and only the client-facing set is published.
# "Building" covers the window in which a function's image is still being built:
# every region's KSVC is failing to pull an image that does not exist yet, and
# that is reported as Building, not Failed, here and in WorkloadStatus alike
# (docs/FUNCTIONS.md - Function Status Resolution).
REGION_STATUSES = ("Ready", "Building", "Deploying", "Failed", "Terminating", "Timeout")
# Machine-readable causes behind a Failed region or rollup, published on /info so
# a UI can switch on them - the Kubernetes reason/message pair, one level up.
# BuildFailed is authoritative (read off the kpack Image); the rest are derived
# from the failing conditions' reason/message - stable-ish Kubernetes and
# Knative codes, but not a contract - so anything unrecognized carries no
# reason and only the raw `message` text.
STATUS_REASONS = (
    "BuildFailed",
    "ImagePullFailed",
    "CrashLooping",
    "ConfigError",
    "ProgressDeadlineExceeded",
)

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

    name: EnvVarName
    value: str | None = None
    secret: bool = False

    @model_validator(mode="after")
    def _non_secret_needs_value(self) -> "EnvVar":
        """A non-secret env var must carry a value (only secrets may keep)."""
        if not self.secret and self.value is None:
            raise ValueError(f"env var '{self.name}' requires a value")
        return self


FileEncoding = Literal["text", "base64"]


class FileMount(BaseModel):
    """An inline file to load into the workload at ``mountPath``.

    The API stores the content in the workload's shared ConfigMap (or Secret when
    ``secret: true``) - one ConfigMap and one Secret per workload - and mounts
    each file at its ``mountPath`` via ``subPath``.

    ``content`` carries the file, and ``encoding`` says how: ``text`` (the
    default) means the string *is* the file, ``base64`` means the string is the
    file's raw bytes base64-encoded - for binary content such as a keystore or a
    DER certificate.

    A secret file may omit ``content``, meaning "keep the stored content" on
    update (the redacted read - ``secret: true, content: null`` - can be sent
    straight back). A non-secret file always needs content.

    The mounted file is always read-only: Kubernetes mounts ConfigMap and Secret
    volumes read-only regardless of what the pod spec asks for.
    """

    mountPath: MountPath
    content: str | None = None
    encoding: FileEncoding = "text"
    secret: bool = False

    @property
    def keep(self) -> bool:
        """Whether this (secret) file keeps its stored content (no content given)."""
        return self.content is None

    @model_validator(mode="after")
    def _check(self) -> "FileMount":
        """Validate the content (present unless a secret keep, decodable if base64).

        Runs at model-parse time, so an undecodable or non-UTF-8 file is rejected
        with a 400 before any service-layer code touches the spec.
        """
        if self.keep and not self.secret:
            raise ValueError("file requires 'content'")
        if self.encoding == "text" and self.content is not None:
            try:
                # A JSON string can carry a lone surrogate, which is not UTF-8.
                self.content.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError(
                    f"file '{self.mountPath}' content is not valid UTF-8 text"
                ) from exc
        if self.encoding == "base64" and self.content is not None:
            try:
                # Lenient: tolerates the line wrapping a PEM body carries, while
                # still rejecting bad padding or a truncated blob.
                base64.b64decode(self.content)
            except ValueError as exc:
                raise ValueError(f"file '{self.mountPath}' has invalid base64 content") from exc
        return self

    def decoded(self) -> bytes:
        """The file's content as raw bytes (``b""`` for a keep).

        Returns:
            ``content`` base64-decoded when ``encoding`` is ``base64``, else its
            UTF-8 encoding.
        """
        if self.content is None:
            return b""
        if self.encoding == "base64":
            return base64.b64decode(self.content)
        return self.content.encode("utf-8")


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

        Built from the same constants the validator enforces (``_KPA_METRICS``,
        the target defaults and bounds, the duration cap), so what is advertised
        is what a create request accepts.

        Returns:
            The per-metric capabilities and the ``scaleDownDelay`` rules.
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


class RegionStatus(BaseModel):
    """The deploy/health state of a workload at a single region.

    The full GET's per-region row. It carries no live cpu/memory usage; that is
    reported by the stats view, on :class:`RegionStats`.

    Attributes:
        region: The region name.
        status: Per-region status (Ready/Deploying/Failed/Terminating/Timeout/...).
        revision: The Knative revision the region is serving, if known.
        reason: Machine-readable cause behind a Failed status, one of
            ``STATUS_REASONS``; None when the cause was not recognized.
        message: The human-readable failure detail when the region failed, else
            None. Kubernetes' reason/message pair: ``reason`` is the word a
            client switches on, this is the text it shows.
        replicas: Running pods at this region (None if unknown).
    """

    region: str
    status: str
    revision: str | None = None
    reason: str | None = None
    message: str | None = None
    replicas: int | None = None


class ResourceUsage(BaseModel):
    """Live resource consumption summed over a workload's running pods."""

    cpu: str | None = None  # e.g. "120m"
    memory: str | None = None  # e.g. "180Mi"


class RegionStats(BaseModel):
    """One region's live state, as the stats view reports it.

    The full GET returns :class:`RegionStatus` instead, which carries the rollout
    detail (``revision``, ``message``) rather than live usage.

    Attributes:
        region: The region name.
        status: Per-region status (Ready/Building/Deploying/Failed/...).
        reason: Machine-readable cause behind a Failed status, one of
            ``STATUS_REASONS``; None when the cause was not recognized.
        replicas: Running pods at this region, or None if unknown.
        usage: Live cpu/memory over those pods; None when scaled to zero or the
            metrics API could not be read.
    """

    region: str
    status: str
    reason: str | None = None
    replicas: int | None = None
    usage: ResourceUsage | None = None


class WorkloadBase(BaseModel):
    """Identity fields common to every workload view (list item and full GET)."""

    name: str
    group: str  # the owning SSO group
    type: Literal["function", "container"]
    hostname: str  # external host (no scheme), e.g. {name}-{group}.{route_domain}
    status: WorkloadStatus
    size: str | None = None  # resource t-shirt size (uniform across regions)
    # workload creation time (metadata.creationTimestamp), in Israel local time
    createdAt: datetime | None = None


class WorkloadSummary(WorkloadBase):
    """Lightweight list item: general info only, no per-region live usage.

    Use the single-workload GET for replicas/usage.
    """

    regions: list[str] = []  # region names where the workload is deployed


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
    always null for secret files. Mirroring :class:`FileMount`, a binary file
    comes back base64-encoded with ``encoding: base64`` - so the read can be
    sent straight back on update, whatever the file holds.
    """

    mountPath: str
    secret: bool = False
    content: str | None = None
    encoding: FileEncoding = "text"


class BuildStatusView(BaseModel):
    """A function's image build state, read from the local region's kpack Image.

    State and message only; the built image itself is not part of any response
    (it is on :class:`common.build.BuildStatus`, which the build service reads).

    Attributes:
        state: Building / Ready / Failed / Unknown.
        message: Why the build failed, when it did.
    """

    state: str
    message: str | None = None


class WorkloadStatsResponse(BaseModel):
    """A workload's live state only - the endpoint to poll.

    Carries what changes on its own and none of the desired-state config, which
    is returned by the full GET.

    Attributes:
        status: The rollup, identical to the full GET's, ``Building`` included.
        reason: The first recognized per-region ``reason``, as on the full GET.
        replicas: Running pods across every region. None if any region's is unknown.
        usage: Cpu/memory across every region. None if any region could not be
            measured.
        regions: One entry per region that has the workload.
    """

    status: WorkloadStatus
    reason: str | None = None
    replicas: int | None = None
    usage: ResourceUsage | None = None
    regions: list[RegionStats] = []


class WorkloadResponse(WorkloadBase):
    """Full single-workload view: identity, live per-region status, and config.

    Identity, live per-region status, and the desired-state config common to both
    offerings (secrets redacted). FunctionResponse and ContainerResponse subclass
    this, so a response mirrors the create body of its offering.
    """

    regions: list[RegionStatus] = []
    statusUrl: str | None = None
    # The first recognized per-region `reason`, repeated at the top level. None
    # when no region's cause was recognized (or nothing failed).
    reason: str | None = None
    # desired-state config common to both offerings (secret values redacted)
    scaling: Scaling | None = None
    env: list[EnvVarView] = []
    files: list[FileView] = []


class PodInfo(BaseModel):
    """One of the workload's pods on the local region.

    What a client needs to pick a pod to follow, and to see why one it was
    following went away. ``usage`` comes from the metrics API and is joined on by
    name, so it is null for a pod too new to have been scraped; the pod is listed
    either way.

    Attributes:
        pod: The pod name - the path segment ``/logs/pods/{pod}`` takes.
        revision: The Knative revision it belongs to, if labelled.
        phase: The pod phase (Running, Pending, Succeeded, Failed, Unknown).
        ready: Whether its Ready condition is true - a Running pod is not
            necessarily serving.
        restarts: Restarts summed over the pod's containers.
        startedAt: When the pod started, in Israel local time.
        usage: Live cpu/memory for this pod's user container(s), excluding the
            queue-proxy sidecar; null when it has not been measured.
    """

    pod: str
    revision: str | None = None
    phase: str
    ready: bool = False
    restarts: int = 0
    startedAt: datetime | None = None
    usage: ResourceUsage | None = None


class PodRoster(BaseModel):
    """The ``pods`` event: which pods the workload has on this region, right now.

    The local region only, like the log streams it feeds. Empty ``pods`` is a
    normal state, not an error: the workload is deployed here and scaled to zero.

    Attributes:
        name: The workload name.
        group: The owning group.
        type: The offering.
        region: The region these pods are on (always the local one).
        pods: The current roster, ordered by name.
    """

    name: str
    group: str
    type: Literal["function", "container"]
    region: str
    pods: list[PodInfo] = []


class LogLine(BaseModel):
    """One line from a followed pod log (the ``log`` event of a logs stream).

    The node's timestamp is split off into ``time``; ``message`` carries the rest
    of the line.

    Attributes:
        pod: The pod the line came from.
        container: The container it was read from.
        revision: The Knative revision the pod belongs to, if labelled.
        time: When the node recorded the line; None if it carried no parseable
            timestamp.
        message: The line itself, without the timestamp prefix.
    """

    pod: str
    container: str
    revision: str | None = None
    time: datetime | None = None
    message: str


class PodLogStreamOpen(BaseModel):
    """The ``open`` event: what this log stream is, sent before any line.

    Attributes:
        name: The workload name.
        group: The owning group.
        type: The offering.
        region: The region the pod is on (always the local one).
        pod: The pod being followed.
        container: The container being read.
        revision: The Knative revision the pod belongs to, if labelled.
    """

    name: str
    group: str
    type: Literal["function", "container"]
    region: str
    pod: str
    container: str
    revision: str | None = None


class PodLogSnapshot(BaseModel):
    """One pod's log as it stands right now (``follow=false``).

    The same ``lines`` a follow would have delivered, returned once and ended -
    for a caller that cannot hold a connection open. What it can return is
    bounded by what the node still holds: Kubernetes keeps no buffer beyond the
    node's rotated file, so this is the recent past, never the whole history.

    Attributes:
        name: The workload name.
        group: The owning group.
        type: The offering.
        region: The region the pod is on (always the local one).
        pod: The pod that was read.
        container: The container it was read from.
        revision: The Knative revision the pod belongs to, if labelled.
        lines: The log, split into lines exactly as the stream splits them.
    """

    name: str
    group: str
    type: Literal["function", "container"]
    region: str
    pod: str
    container: str
    revision: str | None = None
    lines: list[LogLine] = []


class StreamEnd(BaseModel):
    """The ``end`` event: the stream finished on purpose, and why.

    Distinct from ``error``: a pod's log ending is not a failure, it is what a
    scale-down or a new revision looks like. A client can go back to the ``pods``
    stream and pick the pod that replaced it.

    Attributes:
        reason: Why the stream ended, in a form worth showing a user.
    """

    reason: str


class StreamWarning(BaseModel):
    """The ``warning`` event: the stream is degraded but still running.

    Sent when a log stream discards lines because the client is reading too
    slowly, so a gap in the lines is reported rather than silent.

    Attributes:
        message: What was degraded, in a form worth showing a user.
        droppedLines: Lines discarded because the client read too slowly.
    """

    message: str
    droppedLines: int | None = None


class StreamError(BaseModel):
    """The ``error`` event: the stream is ending, and why.

    Once the response has begun there is no status code left to send, so a later
    failure - the workload deleted, a region no longer answering - arrives as
    this event. It carries the same ``code`` vocabulary as the error envelope,
    which ``/info`` publishes.

    Attributes:
        code: The machine-readable error code (e.g. ``NOT_FOUND``).
        message: The human-readable message.
    """

    code: str
    message: str


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
    revision: str | None = None
    # Sub-directory inside the repository; None (or absent) means the root.
    path: str | None = None
