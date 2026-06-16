"""RHBK OIDC token validation: JWKS fetch/cache + JWT verification."""

from __future__ import annotations

import time

import jwt
from jwt import PyJWKClient

from app.core.config import RHBKConfig
from app.core.errors import UnauthenticatedError


class TokenValidator:
    """Validates RHBK-issued JWTs offline against cached JWKS.

    The signing keys are fetched from the internal RHBK realm and cached; no
    per-request round trip to the IdP is made.
    """

    def __init__(self, config: RHBKConfig):
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
