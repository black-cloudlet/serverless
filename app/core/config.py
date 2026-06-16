"""Application settings.

Configuration is environment-driven (12-factor); in production the values are
projected from Vault via the External Secrets Operator (see docs/ARCHITECTURE.md §7).
Zone connection profiles are supplied as a JSON list in ``SERVERLESS_ZONES``.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ZoneConfig(BaseModel):
    """Connection profile for one OpenShift cluster ("zone")."""

    name: str
    api_server: str
    namespace: str = "serverless-workloads"
    # mTLS client identity (cert-manager Certificate, CN = serverless-api.clients.{base_domain})
    client_cert_path: str | None = None
    client_key_path: str | None = None
    ca_path: str | None = None
    # If true, use the in-cluster service account instead of the client cert
    # (used by the local instance talking to its own cluster).
    in_cluster: bool = False


class RHBKConfig(BaseModel):
    """RHBK (Keycloak) OIDC settings used by the auth component."""

    issuer: str = "https://rhbk.internal/realms/serverless"
    audience: str = "serverless-api"
    jwks_url: str = (
        "https://rhbk.internal/realms/serverless/protocol/openid-connect/certs"
    )
    groups_claim: str = "groups"
    admin_groups: list[str] = Field(default_factory=list)
    # Seconds to cache JWKS keys before refetching.
    jwks_cache_seconds: int = 3600


class RegistryConfig(BaseModel):
    """Internal (mirrored) container registry."""

    url: str = "registry.internal"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SERVERLESS_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    app_name: str = "serverless-api"
    auth_enabled: bool = True
    # Single platform wildcard domain; host = {name}-{group}.{route_domain}
    route_domain: str = "serverless.example.com"

    rhbk: RHBKConfig = Field(default_factory=RHBKConfig)
    registry: RegistryConfig = Field(default_factory=RegistryConfig)
    zones: list[ZoneConfig] = Field(default_factory=list)

    def zone(self, name: str) -> ZoneConfig | None:
        return next((z for z in self.zones if z.name == name), None)

    @property
    def zone_names(self) -> list[str]:
        return [z.name for z in self.zones]


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
