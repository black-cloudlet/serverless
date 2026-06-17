"""SSO OIDC token validation: discovery + JWKS fetch/cache + JWT verification."""

from __future__ import annotations

import threading

import httpx
import jwt
from jwt import PyJWKClient

from app.core.config import SSOConfig
from app.core.errors import ServiceUnavailableError, UnauthenticatedError
from app.core.logging import get_logger

logger = get_logger(__name__)


def looks_like_jwt(token: str) -> bool:
    """True if the bearer token is structurally a JWT (vs an opaque API key)."""
    try:
        jwt.get_unverified_header(token)
        return True
    except jwt.PyJWTError:
        return False


class TokenValidator:
    """Validates SSO-issued JWTs offline against cached JWKS.

    The JWKS URI is resolved once from the OIDC ``.well-known`` discovery
    document; a single :class:`PyJWKClient` is then built and reused. It caches
    the JWK set (``jwks_cache_seconds``), so steady-state requests verify
    tokens without any round trip to the IdP.
    """

    def __init__(self, config: SSOConfig):
        self._config = config
        self._jwk_client: PyJWKClient | None = None
        self._lock = threading.Lock()

    def _client(self) -> PyJWKClient:
        # Resolve the JWKS URI from discovery once, then reuse the client. The
        # lock keeps concurrent first requests from each running discovery.
        if self._jwk_client is None:
            with self._lock:
                if self._jwk_client is None:
                    jwks_uri = self._discover_jwks_uri()
                    self._jwk_client = PyJWKClient(
                        jwks_uri,
                        cache_keys=True,
                        lifespan=self._config.jwks_cache_seconds,
                    )
        return self._jwk_client

    def _discover_jwks_uri(self) -> str:
        """Fetch the OIDC discovery document and return its ``jwks_uri``."""
        try:
            resp = httpx.get(
                self._config.discovery_url, timeout=self._config.discovery_timeout
            )
            resp.raise_for_status()
            jwks_uri = resp.json()["jwks_uri"]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise ServiceUnavailableError(
                f"OIDC discovery failed at {self._config.discovery_url}: {exc}"
            ) from exc
        logger.info("Resolved JWKS URI from OIDC discovery: %s", jwks_uri)
        return jwks_uri

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
