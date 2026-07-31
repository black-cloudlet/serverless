"""FastAPI auth dependencies used by the routers."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request

from api.auth.apikey import verify_admin_key
from api.auth.claims import Principal, principal_from_claims
from api.auth.oidc import TokenValidator, looks_like_jwt
from api.core.config import Settings, get_settings
from api.models.common import normalize_group
from common.errors import ForbiddenError, UnauthenticatedError


@lru_cache
def get_validator() -> TokenValidator:
    """The cached OIDC token validator (one JWKS client per process)."""
    return TokenValidator(get_settings().sso)


def _bearer_token(request: Request) -> str:
    """Extract the raw bearer token from the Authorization header.

    Args:
        request: The incoming request.

    Returns:
        The token (the part after ``Bearer ``).

    Raises:
        UnauthenticatedError: If the header is missing or malformed.
    """
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise UnauthenticatedError("Missing or malformed Authorization header.")
    return token


def require_auth(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    validator: Annotated[TokenValidator, Depends(get_validator)],
) -> Principal:
    """Validate the bearer token and return the Principal.

    A structural JWT is validated as an OIDC token; an opaque token is matched
    against the admin API key. When ``auth_enabled`` is false (local dev only) a
    synthetic admin principal is returned so the API is usable without a live SSO.

    Args:
        request: The incoming request (carries the Authorization header).
        settings: Application settings (auth toggle, admin key, SSO config).
        validator: The OIDC token validator.

    Returns:
        The authenticated :class:`Principal`.

    Raises:
        UnauthenticatedError: If the token is missing/malformed or unrecognized.
        ForbiddenError: If a valid OIDC token carries no group membership.
    """
    if not settings.auth_enabled:
        return Principal(subject="dev", username="dev", groups=["dev"], is_admin=True)

    token = _bearer_token(request)

    # A structural JWT -> OIDC user/service token; otherwise an opaque admin API
    # key. The header always carries the RAW token; the API key is matched with a
    # constant-time compare (see api.auth.apikey).
    if looks_like_jwt(token):
        claims = validator.validate(token)
        principal = principal_from_claims(claims, settings.sso)
        if not principal.groups:
            raise ForbiddenError("Token has no group membership.")
        return principal

    if verify_admin_key(token, settings.admin_api_key):
        # Normalized like every other group entering the app, so nothing past the
        # edge ever sees a raw SSO form.
        return Principal(
            subject="admin",
            username="admin",
            groups=[normalize_group(g) for g in settings.sso.admin_groups],
            is_admin=True,
        )

    raise UnauthenticatedError("Invalid bearer token.")


CurrentUser = Annotated[Principal, Depends(require_auth)]
