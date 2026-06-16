"""SSO OIDC token validation: JWKS fetch/cache + JWT verification."""

from __future__ import annotations

import time

import jwt
from jwt import PyJWKClient

from app.core.config import SSOConfig
from app.core.errors import UnauthenticatedError


def looks_like_jwt(token: str) -> bool:
    """True if the bearer token is structurally a JWT (vs an opaque API key)."""
    try:
        jwt.get_unverified_header(token)
        return True
    except jwt.PyJWTError:
        return False


class TokenValidator:
    """Validates SSO-issued JWTs offline against cached JWKS.

    The signing keys are fetched from the internal SSO realm and cached; no
    per-request round trip to the IdP is made.
    """

    def __init__(self, config: SSOConfig):
        self._config = config
        self._jwk_client: PyJWKClient | None = None
        self._jwk_fetched_at: float = 0.0

    def _client(self) -> PyJWKClient:
        now = time.monotonic()
        expired = now - self._jwk_fetched_at > self._config.jwks_cache_seconds
        if self._jwk_client is None or expired:
            self._jwk_client = PyJWKClient(self._config.jwks_url)
            self._jwk_fetched_at = now
        return self._jwk_client

    def validate(self, token: str) -> dict:
        """Return the decoded claims, or raise UnauthenticatedError."""
        try:
            signing_key = self._client().get_signing_key_from_jwt(token)
            return jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._config.audience,
                issuer=self._config.issuer,
                options={"require": ["exp", "iss"]},
            )
        except jwt.PyJWTError as exc:
            raise UnauthenticatedError(f"Invalid token: {exc}") from exc
