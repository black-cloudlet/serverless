"""Request/response schemas for minting a stream ticket."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, field_validator

from api.core.paths import V1, to_internal

# The only paths a ticket may be minted for. Anchored and explicit rather than
# "any path this API serves": a ticket is a bearer credential in a URL, so what
# it can open is enumerated, not inferred. The name/group/pod segments are left
# permissive - authorization is redone from the ticket's own Principal when the
# stream opens, so this is about the shape, not the identity.
# Matched after the mount prefix has been taken off, since the caller writes the
# URL it is going to open rather than the one this app routes on.
STREAM_PATH = re.compile(
    rf"^{V1}/groups/[^/]{{1,63}}/(?:functions|containers)/[^/]{{1,63}}/"
    r"(?:pods|stats/stream|logs/pods/[^/]{1,253})$"
)


class StreamTicketRequest(BaseModel):
    """Ask for a ticket that opens one specific stream.

    Attributes:
        path: The exact path the ticket will be used on, e.g.
            ``/v1/groups/payments/functions/orders/pods``, with or without this
            deployment's mount prefix - both normalize to the same signature.
            Query string excluded - the ticket travels in it.
    """

    path: str

    @field_validator("path")
    @classmethod
    def _known_stream(cls, value: str) -> str:
        """Normalize to the internal path, and reject anything not streamable."""
        value = to_internal(value)
        if not STREAM_PATH.match(value):
            raise ValueError(
                "path must be a streaming endpoint under "
                "/v1/groups/{group}/{functions|containers}/{name}/: "
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
