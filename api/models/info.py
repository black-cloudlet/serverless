"""Public platform-capabilities schema (the /info discovery document)."""

from __future__ import annotations

from pydantic import BaseModel

from api.models.common import ScalingCapabilities


class InfoResponse(BaseModel):
    """Static platform capabilities so a UI can render itself from the server.

    All fields are configuration/code-derived (no cluster calls), so the endpoint
    is safe to serve unauthenticated.

    Attributes:
        version: The running API version.
        sites: The configured site names a workload can target.
        runtimes: The function runtimes available to build from.
        sizes: The resource t-shirt sizes.
        scaling: The per-metric autoscaling options and their bounds.
        routeDomain: The base domain; a custom host must be one label under it.
        defaultHostTemplate: How the default host is composed from a workload's
            name/group and ``routeDomain`` (e.g. ``{name}-{group}.{routeDomain}``),
            so the UI can preview it without hardcoding the rule.
    """

    version: str
    sites: list[str]
    runtimes: list[str]
    sizes: list[str]
    scaling: ScalingCapabilities
    routeDomain: str
    defaultHostTemplate: str
