"""This API's auth wiring: one :class:`SSOAuth` built from its own settings.

The component itself is :mod:`cloudlet_apis.auth`, shared with every API on the
platform; what lives here is only which settings it is built from.

``require_auth`` stays a module-level function wrapping it, so settings resolve
per request and it remains the callable ``dependency_overrides`` keys on.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from cloudlet_apis.auth import Principal, SSOAuth  # noqa: F401 - Principal re-exported
from fastapi import Depends, Request

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
