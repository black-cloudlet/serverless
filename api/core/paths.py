"""Where this API's endpoints live.

There is one path per endpoint and it is the complete one: every router is
registered under :func:`api_base`, every path handed to a client is built from
it, and nothing else answers (docs/API.md - REST API Specification).
"""

from __future__ import annotations

from api.core.config import Settings

# The API's own version segment, applied once where the routers are included.
V1 = "/v1"


def api_base(settings: Settings) -> str:
    """The complete path this API's endpoints are served under.

    The base path comes from ``settings``, not from the process-wide cached
    settings, so a caller holding its own configuration is answered from that
    one.

    Args:
        settings: The settings to read the base path from.

    Returns:
        The base path followed by the version segment - ``/api/serverless/v1``
        as the chart ships it.
    """
    return f"{settings.base_path}{V1}"


def webhook_url(settings: Settings, group: str, name: str) -> str:
    """The URL a git provider is configured to POST a push to.

    The build endpoint, absolute where ``public_url`` is configured: a provider
    is given a URL, not a path, so this is the one place the API states its own
    origin. With none configured it degrades to the path, which is what a local
    run wants and what a caller can still resolve against the host they reached.

    Args:
        settings: The settings carrying the origin and the base path.
        group: The owning group.
        name: The function name.

    Returns:
        The webhook URL.
    """
    return f"{settings.public_url}{api_base(settings)}/groups/{group}/functions/{name}/build"
