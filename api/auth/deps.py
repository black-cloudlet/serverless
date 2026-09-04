"""This API's auth wiring: one :class:`SSOAuth` built from its own settings.

The component itself is :mod:`cloudlet_apis.auth`, shared with every API on the
platform; what lives here is only which settings it is built from.

``require_auth`` is a module-level function wrapping it, so settings resolve per
request and it is the callable ``dependency_overrides`` keys on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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


# What a git provider authenticates a push with. GitLab sends the configured
# secret verbatim in this header, and names the event kind in the other.
GITLAB_TOKEN_HEADER = "X-Gitlab-Token"  # noqa: S105 - a header name, not a credential
GITLAB_EVENT_HEADER = "X-Gitlab-Event"


@dataclass(frozen=True)
class WebhookCaller:
    """A caller who presented a webhook token instead of a bearer.

    Not a :class:`Principal`: nothing is authorized yet. The token is compared
    against the one stored for the function the path names, which is what makes
    it an identity (docs/FUNCTIONS.md - Git webhook).
    """

    # repr=False: a credential must not ride along into a traceback or a log
    # line that prints the caller, exactly as for the git token on a spec.
    token: str = field(repr=False)
    event: str | None = None


def build_caller(
    request: Request, principal: Annotated[Principal | None, Depends(optional_auth)]
) -> Principal | WebhookCaller:
    """Who is asking for a build: an authenticated user, or a git provider.

    A rebuild and a push are the same request, so they share one endpoint and
    differ only in how the caller proves they may make it; a valid bearer wins.
    Through :func:`optional_auth`, not :func:`require_auth`, so a missing header
    falls through to the webhook token rather than raising.

    Args:
        request: The incoming request, for the webhook headers.
        principal: The caller the Authorization header identifies, if any
            (injected).

    Returns:
        The authenticated principal, or the webhook caller.

    Raises:
        UnauthenticatedError: If neither credential is present.
        ForbiddenError: If a valid token carries no group membership.
    """
    if principal is not None:
        return principal
    token = request.headers.get(GITLAB_TOKEN_HEADER)
    if token:
        return WebhookCaller(token=token, event=request.headers.get(GITLAB_EVENT_HEADER))
    raise UnauthenticatedError("missing bearer token or webhook token")


BuildCaller = Annotated[Principal | WebhookCaller, Depends(build_caller)]

# The library's dependency: ticket first, header second (see stream_auth).
# The hint is display text derived from settings and bound at import
# (docs/STREAMING.md - Browsers cannot send an `Authorization` header).
require_stream_auth = stream_auth(
    get_tickets,
    optional_auth,
    mint_path_hint=f"{api_base(get_settings())}{TICKET_MINT_PATH}",
)

StreamUser = Annotated[Principal, Depends(require_stream_auth)]
