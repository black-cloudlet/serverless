"""Which paths a stream ticket may be minted for.

The request/response schemas ship with the flow itself
(:mod:`cloudlet_apis.auth.tickets`); this module holds the allowlist the mint
runs every requested path through (docs/STREAMING.md - Browsers cannot send
an ``Authorization`` header).
"""

from __future__ import annotations

import re

from cloudlet_apis.errors import ValidationError

from api.core.config import get_settings
from api.core.paths import api_base

# The only paths a ticket may be minted for, anchored and enumerated. The
# name/group/pod segments are left permissive: authorization is redone from the
# ticket's own Principal when the stream opens, so this matches the shape of a
# path, not the identity in it.
_STREAM_SUFFIX = (
    r"/groups/[^/]{1,63}/(?:functions|containers)/[^/]{1,63}/"
    r"(?:pods|stats/stream|logs/pods/[^/]{1,253})$"
)


def stream_path_pattern() -> re.Pattern[str]:
    """The streaming paths, anchored at where this API is actually served.

    The base path is read from settings on every call, so a changed setting takes
    effect immediately; ``re`` caches the compilation.

    Returns:
        The compiled pattern a mintable path must match.
    """
    return re.compile(f"^{re.escape(api_base(get_settings()))}{_STREAM_SUFFIX}")


def validate_stream_path(path: str) -> str:
    """The allowlist the mint endpoint runs every requested path through.

    Args:
        path: The path the caller wants a ticket for.

    Returns:
        The path, if it names one of this API's streaming endpoints.

    Raises:
        ValidationError: If it does not.
    """
    if not stream_path_pattern().match(path):
        raise ValidationError(
            "path must be a streaming endpoint under "
            f"{api_base(get_settings())}/groups/{{group}}/{{functions|containers}}/{{name}}/: "
            "'pods', 'stats/stream', or 'logs/pods/{pod}'"
        )
    return path
