"""Minting the tickets browsers open streams with."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from cloudlet_apis.auth import StreamTickets
from fastapi import APIRouter, Depends

from api.auth.deps import CurrentUser, get_tickets
from api.models.stream import StreamTicketRequest, StreamTicketResponse
from api.services.state.ksvc_state import ISRAEL_TZ

router = APIRouter(prefix="/api/v1", tags=["streams"])


@router.post("/stream-tickets", response_model=StreamTicketResponse)
async def create_stream_ticket(
    body: StreamTicketRequest,
    user: CurrentUser,
    tickets: Annotated[StreamTickets, Depends(get_tickets)],
) -> StreamTicketResponse:
    """Mint a short-lived ticket for one streaming endpoint.

    For browsers only. ``EventSource`` cannot send an ``Authorization`` header,
    so the token is spent here - on a POST, which can carry one - for a ticket
    that opens exactly one stream and expires in about a minute.

    No group check: the ticket carries the caller's own identity and nothing
    more, and the stream re-runs the same authorization the non-streaming
    endpoints do. A ticket for a group you are not in opens a stream that 404s,
    exactly as the GET would.

    Args:
        body: The stream path the ticket is for.
        user: The authenticated caller (injected).
        tickets: The ticket signer (injected).

    Returns:
        The ticket and its expiry.

    Raises:
        ServiceUnavailableError: If this deployment has no signing key
            configured, in which case streams take the Authorization header only.
    """
    ticket = tickets.mint(user, body.path)
    return StreamTicketResponse(
        ticket=ticket.value,
        expiresAt=datetime.fromtimestamp(ticket.expires_at, tz=ISRAEL_TZ),
        path=body.path,
    )
