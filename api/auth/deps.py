"""This API's auth wiring: one :class:`SSOAuth` built from its own settings.

The component itself is :mod:`cloudlet_apis.auth`, shared with every API on the
platform; what lives here is only which settings it is built from.

``require_auth`` is a module-level function wrapping it, so settings resolve per
request and it is the callable ``dependency_overrides`` keys on.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from cloudlet_apis.auth import (  # noqa: F401 - Principal re-exported
    Principal,
    SSOAuth,
    StreamTickets,
    stream_auth,
)
from cloudlet_apis.auth.tickets import TICKET_MINT_PATH
from cloudlet_apis.errors import UnauthenticatedError
from fastapi import Depends, Request

from api.core.config import get_settings
from api.core.paths import api_base


@lru_cache
def get_auth() -> SSOAuth:
    """The app's auth component (one JWKS cache per process).

    Cached here, so one validator and one OIDC discovery round trip serve every
    request. ``api.main`` warms it during startup.

    Returns:
        The configured :class:`~cloudlet_apis.auth.SSOAuth`.
    """
    settings = get_settings()
    return SSOAuth(
        settings.sso,
        admin_api_key=settings.admin_api_key,
        enabled=settings.auth_enabled,
    )


def require_auth(request: Request) -> Principal:
    """Validate the bearer token and return the Principal.

    Args:
        request: The incoming request (carries the Authorization header).

    Returns:
        The authenticated :class:`~cloudlet_apis.auth.Principal`.

    Raises:
        UnauthenticatedError: If the token is missing/malformed or unrecognized.
        ForbiddenError: If a valid OIDC token carries no group membership.
    """
    return get_auth().require_auth(request)


CurrentUser = Annotated[Principal, Depends(require_auth)]


@lru_cache
def get_tickets() -> StreamTickets:
    """The app's stream-ticket signer (one per process, from the configured key)."""
    settings = get_settings()
    return StreamTickets(settings.stream_ticket_key, ttl_seconds=settings.stream.ticket_ttl_seconds)


def optional_auth(request: Request) -> Principal | None:
    """The caller the Authorization header identifies, or None if there is none.

    The stream endpoints take either a ticket or a header, so a missing header
    is a failure there only once the ticket has also come up empty. It is a
    dependency of its own, so FastAPI resolves it by identity and
    ``dependency_overrides`` can replace the header half on its own.

    Args:
        request: The incoming request.

    Returns:
        The authenticated principal, or None if the header carried no usable
        credential. With auth disabled this is the dev principal, not None.

    Raises:
        ForbiddenError: If a valid OIDC token carries no group membership. It
            propagates instead of becoming None.
    """
    try:
        return require_auth(request)
    except UnauthenticatedError:
        return None


# The library's dependency: ticket first, header second (see stream_auth).
# The hint is display text derived from settings and bound at import
# (docs/ARCHITECTURE.md - Browsers cannot send an `Authorization` header).
require_stream_auth = stream_auth(
    get_tickets,
    optional_auth,
    mint_path_hint=f"{api_base(get_settings())}{TICKET_MINT_PATH}",
)

StreamUser = Annotated[Principal, Depends(require_stream_auth)]
