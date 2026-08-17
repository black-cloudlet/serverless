"""This API's auth wiring: one :class:`SSOAuth` built from its own settings.

The component itself is :mod:`cloudlet_apis.auth`, shared with every API on the
platform; what lives here is only which settings it is built from.

``require_auth`` stays a module-level function wrapping it, so settings resolve
per request and it remains the callable ``dependency_overrides`` keys on.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from cloudlet_apis.auth import (  # noqa: F401 - Principal re-exported
    Principal,
    SSOAuth,
    StreamTickets,
)
from cloudlet_apis.errors import UnauthenticatedError
from fastapi import Depends, Query, Request

from api.core.config import get_settings


@lru_cache
def get_auth() -> SSOAuth:
    """The app's auth component (one JWKS cache per process).

    Cached here rather than inside the component: building it per request would
    give a fresh validator, and a fresh discovery round trip, every time.

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

    Exists because the stream endpoints have two ways in, and "no header" is
    only a failure there once the ticket has also come up empty. A dependency of
    its own rather than a ``try`` inside the one below: FastAPI resolves this by
    identity, so a test - or any app wiring - overrides the header half exactly
    the way it overrides :func:`require_auth` everywhere else.

    Args:
        request: The incoming request.

    Returns:
        The authenticated principal, or None if the header carried no usable
        credential. With auth disabled this is the dev principal, not None.

    Raises:
        ForbiddenError: If a valid OIDC token carries no group membership - a
            caller who authenticated and is not allowed is not a caller who
            failed to authenticate.
    """
    try:
        return require_auth(request)
    except UnauthenticatedError:
        return None


def require_stream_auth(
    request: Request,
    header_user: Annotated[Principal | None, Depends(optional_auth)],
    tickets: Annotated[StreamTickets, Depends(get_tickets)],
    ticket: Annotated[
        str | None,
        Query(
            description=(
                "A ticket from POST /api/serverless/v1/stream-tickets, for browsers: "
                "EventSource "
                "cannot send an Authorization header. Clients that can send one should, "
                "and omit this."
            )
        ),
    ] = None,
) -> Principal:
    """Authenticate a stream by ticket, or by the ordinary Authorization header.

    Ticket first, and deliberately without a fallback: a caller that sent one is
    a browser, which has no second way to authenticate. Falling through to the
    header would answer a bad ticket with a 401 about a missing header, sending
    whoever debugs it after the wrong thing entirely.

    Args:
        request: The incoming request.
        header_user: The caller the Authorization header identifies, if any
            (injected).
        tickets: The ticket signer (injected, like the router that mints them -
            calling the cached factory here instead would put the one component
            with a key in it beyond the reach of the app's own wiring).
        ticket: The ``?ticket=`` value, if the caller is using one.

    Returns:
        The authenticated :class:`~cloudlet_apis.auth.Principal`.

    Raises:
        UnauthenticatedError: If the ticket is invalid, or neither credential
            was supplied.
        ForbiddenError: If a valid OIDC token carries no group membership.
    """
    if ticket is not None:
        # Verified against the path alone. The query string holds the ticket
        # itself, so signing over it would mean signing over the signature.
        return tickets.verify(ticket, request.url.path)
    if header_user is None:
        raise UnauthenticatedError(
            "Missing or malformed Authorization header. A browser should open this "
            "stream with a ticket from POST /api/serverless/v1/stream-tickets instead."
        )
    return header_user


StreamUser = Annotated[Principal, Depends(require_stream_auth)]
