"""Minting the tickets browsers open streams with.

The endpoint itself is :func:`cloudlet_apis.auth.ticket_mint_router`; what is
wired here is everything that makes it this API's: its authentication, its
path allowlist, and its timezone. Mounted under the base path in ``main.py``
like every router, so the mint lands at ``{api_base}/stream-tickets``.
"""

from __future__ import annotations

from cloudlet_apis.auth import ticket_mint_router

from api.auth.deps import get_tickets, require_auth
from api.models.stream import validate_stream_path
from api.services.state.ksvc_state import ISRAEL_TZ

router = ticket_mint_router(
    get_tickets,
    require_auth,
    validate_stream_path,
    # Israel local, like every other timestamp this API returns.
    expiry_tz=ISRAEL_TZ,
)
