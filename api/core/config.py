"""API service settings.

Configuration is environment-driven (12-factor); in production the values are
projected from Vault via the External Secrets Operator (see docs/ARCHITECTURE.md §7).
The connection identity (sites, CA bundle, client cert, registry, timeouts) is
shared and lives in :mod:`common.config`; this module adds the API's own fields.
Site connection profiles are supplied as a JSON list in ``SERVERLESS_SITES``.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel, Field

# Shared connection settings + sub-configs; re-exported for existing importers.
from common.config import (  # noqa: F401
    CABundleConfig,
    CommonSettings,
    RegistryConfig,
    SiteConfig,
)


class SSOConfig(BaseModel):
    """SSO (Keycloak) OIDC settings used by the auth component.

    Only the ``issuer`` is configured; the discovery, authorization and token
    endpoints are all fixed Keycloak paths under it and are derived as properties.
    """

    issuer: str = "https://sso.internal/realms/serverless"
    # Expected token audience. When set, the `aud` claim is verified to contain it;
    # when empty (the default), audience verification is skipped - so tokens work
    # without a Keycloak audience mapper unless an audience is explicitly configured.
    audience: str = ""
    # Public Keycloak client Swagger UI uses for its "Authorize" login
    # (Authorization Code + PKCE; no secret). From Helm values, not a secret.
    swagger_client_id: str = "serverless-api-swagger"
    groups_claim: str = "groups"
    admin_groups: list[str] = Field(default_factory=list)
    # Seconds the JWK set is cached before it is refetched from the JWKS URI.
    jwks_cache_seconds: int = 3600
    # Timeout (seconds) for the one-off OIDC discovery request.
    discovery_timeout: float = 5.0

    @property
    def discovery_url(self) -> str:
        """The OIDC discovery document URL (issuer + the fixed well-known path)."""
        return f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"

    @property
    def authorization_url(self) -> str:
        """The Keycloak authorization endpoint (derived from the issuer)."""
        return f"{self.issuer.rstrip('/')}/protocol/openid-connect/auth"

    @property
    def token_url(self) -> str:
        """The Keycloak token endpoint (derived from the issuer)."""
        return f"{self.issuer.rstrip('/')}/protocol/openid-connect/token"


class Settings(CommonSettings):
    """API settings: the shared connection settings plus the API's own fields."""

    app_name: str = "serverless-api"
    port: int = 8080
    auth_enabled: bool = True
    # Single platform wildcard domain; host = {name}-{group}.{route_domain}
    route_domain: str = "serverless.example.com"

    # Browser origins allowed to call the API (e.g. the ServiceNow portal).
    # Empty disables CORS. env: SERVERLESS_CORS_ALLOW_ORIGINS (JSON list).
    cors_allow_origins: list[str] = Field(default_factory=list)

    # Available function runtimes, mounted as a YAML file from a ConfigMap. Absent
    # in local dev/tests -> the loader falls back to built-in defaults.
    runtimes_file: str = "/etc/serverless/runtimes/runtimes.yaml"

    sso: SSOConfig = Field(default_factory=SSOConfig)
    # Static admin API key for non-OIDC service accounts (opaque bearer token).
    # The RAW token, sourced from Vault via ESO (env SERVERLESS_ADMIN_API_KEY).
    # Matched with a constant-time compare (see api/auth/apikey.py). Defaults to
    # empty, which disables API-key auth (OIDC stays primary); never ship a usable
    # default credential. Set it to enable key auth for admin automation.
    admin_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
