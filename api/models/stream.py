"""Request/response schemas for minting a stream ticket."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, field_validator

# The only paths a ticket may be minted for. Anchored and explicit rather than
# "any path this API serves": a ticket is a bearer credential in a URL, so what
# it can open is enumerated, not inferred. The name/group segments are left
# permissive - authorization is redone from the ticket's own Principal when the
# stream opens, so this is about the shape, not the identity.
STREAM_PATH = re.compile(
    r"^/api/v1/groups/[^/]{1,63}/(?:functions|containers)/[^/]{1,63}/(?:logs|stats)/stream$"
)


class StreamTicketRequest(BaseModel):
    """Ask for a ticket that opens one specific stream.

    Attributes:
        path: The exact path the ticket will be used on, e.g.
            ``/api/v1/groups/payments/functions/orders/logs/stream``. Query
            string excluded - the ticket travels in it.
    """

    path: str

    @field_validator("path")
    @classmethod
    def _known_stream(cls, value: str) -> str:
        """Reject a path that is not one of the streaming endpoints."""
        if not STREAM_PATH.match(value):
            raise ValueError(
                "path must be a streaming endpoint, "
                "/api/v1/groups/{group}/{functions|containers}/{name}/{logs|stats}/stream"
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
