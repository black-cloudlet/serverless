"""Public platform-capabilities schemas (the per-offering /info documents)."""

from __future__ import annotations

from pydantic import BaseModel

from api.models.common import ScalingCapabilities


class RuntimeCapability(BaseModel):
    """One runtime a function may be built with, and the versions it offers.

    Projected from the runtimes ConfigMap the builder reads, so the advertised
    versions are the ones a build will actually accept.

    Attributes:
        name: The value to send as ``runtime``.
        versions: Selectable language versions; empty when the runtime pins one.
        defaultVersion: Applied when the caller picks none.
    """

    name: str
    versions: list[str] = []
    defaultVersion: str | None = None


class NamingRule(BaseModel):
    """The limit on ``name`` and ``group`` *together*.

    No per-field schema can express this: the halves are validated separately
    and each may be 63 characters, but it is their join that becomes the KSVC
    name and the first DNS label. ``group`` is a path parameter besides, so it
    is not even in the body being validated. Published so a form can catch the
    pair before submitting, instead of the API rejecting it at the edge.

    Attributes:
        template: How the object name is composed.
        maxLength: Longest permitted result, in characters.
    """

    template: str
    maxLength: int


class ErrorCode(BaseModel):
    """One machine-readable error code and the HTTP status carrying it.

    Attributes:
        code: The ``error.code`` value in the envelope.
        status: The HTTP status it is returned with.
    """

    code: str
    status: int


class StatusVocabulary(BaseModel):
    """The status strings a client can receive, so none has to be hardcoded.

    Attributes:
        workload: Values of the workload ``status`` - what a poller switches on.
        region: Values of a per-region ``status`` inside ``regions[]``.
        terminal: The subset of ``workload`` that a poller should stop on;
            anything else is still in flight.
        reasons: Values of the workload and per-region ``reason`` fields - the
            machine-readable causes behind a failure. Best-effort: a failure
            whose cause was not recognized carries no reason, only ``error``.
    """

    workload: list[str]
    region: list[str]
    terminal: list[str]
    reasons: list[str] = []


class PortCapability(BaseModel):
    """The container port field's rules, so a UI can render/validate it.

    Attributes:
        required: Whether a port must be supplied on create/update.
        default: Applied when the caller sends none - the port Knative injects
            as ``$PORT``. Published so a form can pre-fill it instead of
            hardcoding the number.
        min: The smallest accepted port.
        max: The largest accepted port.
    """

    required: bool
    default: int
    min: int
    max: int


class BaseInfo(BaseModel):
    """Platform capabilities common to every offering.

    All fields are configuration/code-derived (no cluster calls), so the info
    endpoints are safe to serve unauthenticated.

    Attributes:
        version: The running API version.
        regions: The configured region names a workload can target.
        sizes: The resource t-shirt sizes.
        scaling: The per-metric autoscaling options and their bounds.
        routeDomain: The base domain; a custom host must be one label under it.
        defaultHostTemplate: How the default host is composed from a workload's
            name/group and ``routeDomain`` (e.g. ``{name}-{group}.{routeDomain}``),
            so the UI can preview it without hardcoding the rule.
        statuses: The status strings a response can carry.
        errorCodes: Every ``error.code`` an error envelope can carry.
        naming: The combined limit on name and group.
    """

    version: str
    regions: list[str]
    sizes: list[str]
    scaling: ScalingCapabilities
    routeDomain: str
    defaultHostTemplate: str
    statuses: StatusVocabulary
    errorCodes: list[ErrorCode]
    naming: NamingRule


class ContainerInfoResponse(BaseInfo):
    """Capabilities for creating a container (bring-your-own image).

    Attributes:
        port: The container port rules (bounds + the applied default).
    """

    port: PortCapability


class FunctionInfoResponse(BaseInfo):
    """Capabilities for creating a function (build-from-source).

    Attributes:
        runtimes: The runtimes a function may be built with, each with its
            selectable versions.
        port: The container port rules - identical to the container document's.
    """

    runtimes: list[RuntimeCapability]
    port: PortCapability
