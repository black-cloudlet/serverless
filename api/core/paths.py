"""Where this API's endpoints live.

There is one path per endpoint and it is the complete one. Everything is
registered under :func:`api_base`, every path handed to a client is built from
it, and nothing else answers - so no part of the code holds a second, shorter
spelling that has to be translated to or from.
"""

from __future__ import annotations

from api.core.config import get_settings

# The API's own version segment. Applied once, where the routers are included,
# so a v2 is a second include rather than an edit to every router module.
V1 = "/v1"


def api_base() -> str:
    """The complete path this API's endpoints are served under.

    Returns:
        The configured base path followed by the version segment -
        ``/api/serverless/v1`` as the chart ships it.
    """
    return f"{get_settings().base_path}{V1}"
