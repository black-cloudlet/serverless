"""Request/response schemas for minting a stream ticket."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, field_validator

from api.core.config import get_settings
from api.core.paths import api_base

# The only paths a ticket may be minted for. Anchored and explicit rather than
# "any path this API serves": a ticket is a bearer credential in a URL, so what
# it can open is enumerated, not inferred. The name/group/pod segments are left
# permissive - authorization is redone from the ticket's own Principal when the
# stream opens, so this is about the shape, not the identity.
_STREAM_SUFFIX = (
    r"/groups/[^/]{1,63}/(?:functions|containers)/[^/]{1,63}/"
    r"(?:pods|stats/stream|logs/pods/[^/]{1,253})$"
)


def stream_path_pattern() -> re.Pattern[str]:
    """The streaming paths, anchored at where this API is actually served.

    Built per call rather than at import: the base is configuration, and the
    tests that vary it would otherwise be matching against the first value ever
    read. ``re`` caches the compilation.

    Returns:
        The compiled pattern a mintable path must match.
    """
    return re.compile(f"^{re.escape(api_base(get_settings()))}{_STREAM_SUFFIX}")


class StreamTicketRequest(BaseModel):
    """Ask for a ticket that opens one specific stream.

    Attributes:
        path: The exact path the ticket will be used on - the complete one, as
            the caller will open it, e.g. one under
            ``/api/serverless/v1/groups/{group}/functions/{name}/``. Query
            string excluded: the ticket travels in it.
    """

    path: str

    @field_validator("path")
    @classmethod
    def _known_stream(cls, value: str) -> str:
        """Reject a path that is not one of the streaming endpoints."""
        if not stream_path_pattern().match(value):
            raise ValueError(
                "path must be a streaming endpoint under "
                f"{api_base(get_settings())}/groups/{{group}}/{{functions|containers}}/{{name}}/: "
                "'pods', 'stats/stream', or 'logs/pods/{pod}'"
            )
        return value


class StreamTicketResponse(BaseModel):
    """A minted ticket and its expiry.

    Attributes:
        ticket: Send as ``?ticket=`` on the path it was minted for.
        expiresAt: When it stops being accepted, in Israel local time like every
            other timestamp the API returns.
        path: Echoed back, since the ticket is only valid for this one.
    """

    ticket: str
    expiresAt: datetime
    path: str
