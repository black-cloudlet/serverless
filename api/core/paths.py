"""Where this API's endpoints live.

There is one path per endpoint and it is the complete one. Everything is
registered under :func:`api_base`, every path handed to a client is built from
it, and nothing else answers - so no part of the code holds a second, shorter
spelling that has to be translated to or from.
"""

from __future__ import annotations

from api.core.config import Settings

# The API's own version segment. Applied once, where the routers are included,
# so a v2 is a second include rather than an edit to every router module.
V1 = "/v1"


def api_base(settings: Settings) -> str:
    """The complete path this API's endpoints are served under.

    Takes the settings rather than reading the cached ones, so a caller that
    holds its own - every service does - cannot be answered from a different
    configuration than the one it was built with.

    Args:
        settings: The settings to read the base path from.

    Returns:
        The base path followed by the version segment - ``/api/serverless/v1``
        as the chart ships it.
    """
    return f"{settings.base_path}{V1}"
