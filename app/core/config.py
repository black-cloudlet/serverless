"""Application settings.

Configuration is environment-driven (12-factor); in production the values are
projected from Vault via the External Secrets Operator (see docs/ARCHITECTURE.md §7).
Site connection profiles are supplied as a JSON list in ``SERVERLESS_SITES``.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SiteConfig(BaseModel):
    """Connection profile for one site.

    A *site* is a region (e.g. ``central``, ``south``); it runs an OpenShift
    *cluster* (e.g. ``central-0``) reachable at ``api_server`` (e.g.
    ``https://api.central-0.{base_domain}:6443``). The client certificate, CA
    bundle, and workloads namespace are global (the same in every cluster).
    """

    name: str  # site/region, e.g. "central"
    api_server: str
    cluster: str | None = None  # cluster instance name, e.g. "central-0"


class CABundleConfig(BaseModel):
    """The OpenShift-injected trusted CA bundle ConfigMap.

    OpenShift populates a ConfigMap labelled
    ``config.openshift.io/inject-trusted-cabundle: "true"`` with the cluster's
    trusted CAs. We mount it into the API and every workload; it is the same for
    every cluster, so the API's Kubernetes client also uses it to verify the API
    servers.
    """

    config_map: str = "trusted-ca-bundle"
    key: str = "ca-bundle.crt"
    mount_path: str = "/etc/serverless/trusted-ca"

    @property
    def path(self) -> str:
        return f"{self.mount_path.rstrip('/')}/{self.key}"


class SSOConfig(BaseModel):
    """SSO (Keycloak) OIDC settings used by the auth component."""

    issuer: str = "https://sso.internal/realms/serverless"
    audience: str = "serverless-api"
    jwks_url: str = (
        "https://sso.internal/realms/serverless/protocol/openid-connect/certs"
    )
    groups_claim: str = "groups"
    admin_groups: list[str] = Field(default_factory=list)
    # Seconds to cache JWKS keys before refetching.
    jwks_cache_seconds: int = 3600


class ApiKey(BaseModel):
    """A static, platform-issued API key for a non-OIDC service account.

    Only the **sha256 hash** of the key is stored (hex); the raw key is never
    persisted. Each key maps to an identity and SSO groups for authorization.
    """

    name: str
    sha256: str
    groups: list[str] = Field(default_factory=list)


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

    # Browser origins allowed to call the API (e.g. the ServiceNow portal).
    # Empty disables CORS. env: SERVERLESS_CORS_ALLOW_ORIGINS (JSON list).
    cors_allow_origins: list[str] = Field(default_factory=list)

    # Directory holding the cert-manager client cert (tls.crt / tls.key), used to
    # authenticate to every cluster (env: SERVERLESS_CLIENT_CERT_DIR).
    client_cert_dir: str = "/etc/serverless/client"
    # Trusted CA bundle (env: SERVERLESS_CA_BUNDLE__*).
    ca_bundle: CABundleConfig = Field(default_factory=CABundleConfig)

    # Per-call timeouts to a cluster's API server (seconds). Without these a down
    # cluster would block a worker thread until the OS socket timeout.
    cluster_connect_timeout: float = 5.0
    cluster_read_timeout: float = 15.0
    # Backstop for a whole per-site operation (covers several sequential calls).
    site_op_timeout: float = 60.0

    # Namespace (same in every cluster) where the API creates workloads.
    workloads_namespace: str = "serverless-workloads"

    sso: SSOConfig = Field(default_factory=SSOConfig)
    registry: RegistryConfig = Field(default_factory=RegistryConfig)
    sites: list[SiteConfig] = Field(default_factory=list)
    # Static API keys (sha256-hashed) for non-OIDC service accounts. From Vault
    # via ESO. env: SERVERLESS_API_KEYS (JSON list).
    api_keys: list[ApiKey] = Field(default_factory=list)

    @property
    def client_cert_path(self) -> str:
        return f"{self.client_cert_dir.rstrip('/')}/tls.crt"

    @property
    def client_key_path(self) -> str:
        return f"{self.client_cert_dir.rstrip('/')}/tls.key"

    def site(self, name: str) -> SiteConfig | None:
        return next((z for z in self.sites if z.name == name), None)

    @property
    def site_names(self) -> list[str]:
        return [z.name for z in self.sites]


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
