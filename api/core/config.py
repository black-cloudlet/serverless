"""API service settings.

Environment-driven; in production the values come from Vault via the External
Secrets Operator. The connection identity is shared and lives in
:mod:`common.config`, the SSO model in :mod:`cloudlet_apis.auth`; this module
adds the API's own fields and this deployment's defaults for both.
"""

from __future__ import annotations

from functools import lru_cache

# The SSO model is shared - every API on the platform validates tokens the same
# way - so it lives in cloudlet_apis and is re-exported here for existing importers.
from cloudlet_apis.auth import SSOConfig  # noqa: F401
from pydantic import Field

# Shared connection settings + sub-configs; re-exported for existing importers.
from common.config import (  # noqa: F401
    CABundleConfig,
    CommonSettings,
    RegistryConfig,
    SiteConfig,
)


class SSOSettings(SSOConfig):
    """The shared SSO model with this deployment's defaults filled in.

    ``SSOConfig.issuer`` is required: a package shared by every API must not
    carry one environment's identity provider as a silent fallback, since that
    is the value deciding whose signatures a service trusts. Ours is a property
    of this deployment, so the default is re-declared here, where it is ours to
    be wrong about.

    A subclass rather than a ``default_factory`` returning a populated model:
    pydantic-settings builds the nested model from the env vars it finds, so with
    a factory a single ``SERVERLESS_SSO__ADMIN_GROUPS`` would construct an
    ``SSOConfig`` with no issuer at all and fail validation. Defaults declared on
    the field survive a partial override; a factory's do not.
    """

    issuer: str = "https://sso.internal/realms/serverless"
    # Public Keycloak client Swagger UI uses for its "Authorize" login
    # (Authorization Code + PKCE; no secret). From Helm values, not a secret.
    swagger_client_id: str = "serverless-api-swagger"


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

    sso: SSOSettings = Field(default_factory=SSOSettings)
    # Raw admin key from Vault via ESO. Empty (the default) disables key auth
    # rather than shipping a usable default credential.
    admin_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
