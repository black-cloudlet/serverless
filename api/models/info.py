"""Public platform-capabilities schema (the /info discovery document)."""

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


class ContainerCapabilities(BaseModel):
    """Create options specific to the container offering (bring-your-own image)."""

    port: PortCapability


class FunctionCapabilities(BaseModel):
    """Create options specific to the function offering (build-from-source)."""

    runtimes: list[str]


class OfferingCapabilities(BaseModel):
    """The per-offering capabilities: what differs between containers and functions."""

    container: ContainerCapabilities
    function: FunctionCapabilities


class InfoResponse(BaseModel):
    """Static platform capabilities so a UI can render itself from the server.

    All fields are configuration/code-derived (no cluster calls), so the endpoint
    is safe to serve unauthenticated. Options common to both offerings live at the
    top level; the bits that differ per offering live under ``offerings``.

    Attributes:
        version: The running API version.
        sites: The configured site names a workload can target.
        sizes: The resource t-shirt sizes.
        scaling: The per-metric autoscaling options and their bounds.
        routeDomain: The base domain; a custom host must be one label under it.
        defaultHostTemplate: How the default host is composed from a workload's
            name/group and ``routeDomain`` (e.g. ``{name}-{group}.{routeDomain}``),
            so the UI can preview it without hardcoding the rule.
        offerings: Per-offering create options - the container ``port`` rules and
            the function ``runtimes`` - that don't apply to both.
    """

    version: str
    sites: list[str]
    sizes: list[str]
    scaling: ScalingCapabilities
    routeDomain: str
    defaultHostTemplate: str
    offerings: OfferingCapabilities
