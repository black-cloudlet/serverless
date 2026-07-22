"""Public platform-capabilities schemas (the per-offering /info documents)."""

from __future__ import annotations

from pydantic import BaseModel

from api.models.common import ScalingCapabilities


class PortCapability(BaseModel):
    """The container port field's rules, so a UI can render/validate it.

    Attributes:
        required: Whether a port must be supplied on create/update.
        min: The smallest accepted port.
        max: The largest accepted port.
    """

    required: bool
    min: int
    max: int


class BaseInfo(BaseModel):
    """Platform capabilities common to every offering.

    All fields are configuration/code-derived (no cluster calls), so the info
    endpoints are safe to serve unauthenticated.

    Attributes:
        version: The running API version.
        sites: The configured site names a workload can target.
        sizes: The resource t-shirt sizes.
        scaling: The per-metric autoscaling options and their bounds.
        routeDomain: The base domain; a custom host must be one label under it.
        defaultHostTemplate: How the default host is composed from a workload's
            name/group and ``routeDomain`` (e.g. ``{name}-{group}.{routeDomain}``),
            so the UI can preview it without hardcoding the rule.
    """

    version: str
    sites: list[str]
    sizes: list[str]
    scaling: ScalingCapabilities
    routeDomain: str
    defaultHostTemplate: str


class ContainerInfoResponse(BaseInfo):
    """Capabilities for creating a container (bring-your-own image).

    Attributes:
        port: The container port rules (required + bounds).
    """

    port: PortCapability


class FunctionInfoResponse(BaseInfo):
    """Capabilities for creating a function (build-from-source).

    Attributes:
        runtimes: The runtimes a function may be built with.
    """

    runtimes: list[str]
