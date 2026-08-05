"""Public platform-capabilities schemas (the per-offering /info documents)."""

from __future__ import annotations

from pydantic import BaseModel

from api.models.common import ScalingCapabilities


class RuntimeCapability(BaseModel):
    """One runtime a function may be built with, and the versions it offers.

    Projected from the runtimes ConfigMap the builder reads, so the advertised
    versions are the ones a build will actually accept.
    """

    name: str
    versions: list[str] = []
    defaultVersion: str | None = None


class NamingRule(BaseModel):
    """The limit on ``name`` and ``group`` *together*."""

    template: str
    maxLength: int


class ErrorCode(BaseModel):
    """One machine-readable error code and the HTTP status carrying it."""

    code: str
    status: int


class StatusVocabulary(BaseModel):
    """The status strings a client can receive, so none has to be hardcoded."""

    workload: list[str]
    site: list[str]
    terminal: list[str]


class PortCapability(BaseModel):
    """The container port field's rules, so a UI can render/validate it."""

    required: bool
    default: int
    min: int
    max: int


class BaseInfo(BaseModel):
    """Platform capabilities common to every offering.

    All fields are configuration/code-derived (no cluster calls), so the info
    endpoints are safe to serve unauthenticated.
    """

    version: str
    sites: list[str]
    sizes: list[str]
    scaling: ScalingCapabilities
    routeDomain: str
    defaultHostTemplate: str
    statuses: StatusVocabulary
    errorCodes: list[ErrorCode]
    naming: NamingRule


class ContainerInfoResponse(BaseInfo):
    """Capabilities for creating a container (bring-your-own image)."""

    port: PortCapability


class FunctionInfoResponse(BaseInfo):
    """Capabilities for creating a function (build-from-source)."""

    runtimes: list[RuntimeCapability]
    port: PortCapability
