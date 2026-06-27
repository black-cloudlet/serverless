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
    *cluster* (e.g. ``central-0``) whose API server is derived as
    ``https://api.{cluster}.{base_domain}:6443``. The client certificate, CA
    bundle, and workloads namespace are global (the same in every cluster).
    """

    name: str  # site/region, e.g. "central"
    cluster: str  # cluster instance name, e.g. "central-0"


class CABundleConfig(BaseModel):
    """The OpenShift-injected trusted CA bundle ConfigMap.

    OpenShift populates a ConfigMap labelled
    ``config.openshift.io/inject-trusted-cabundle: "true"`` with the cluster's
    trusted CAs. We mount it into the API and every workload; it is the same for
    every cluster, so the API's Kubernetes client also uses it to verify the API
    servers.
    """

    config_map: str = "ca-bundle"
    key: str = "ca-bundle.crt"
    mount_path: str = "/etc/ssl/certs"

    @property
    def file(self) -> str:
        """Absolute path to the mounted CA bundle file (``mount_path/key``)."""
        return f"{self.mount_path.rstrip('/')}/{self.key}"


class SSOConfig(BaseModel):
    """SSO (Keycloak) OIDC settings used by the auth component.

    Only the ``issuer`` is configured; the discovery, authorization and token
    endpoints are all fixed Keycloak paths under it and are derived as properties.
    """

    issuer: str = "https://sso.internal/realms/serverless"
    # Expected token audience. When set, the `aud` claim is verified to contain it;
    # when empty (the default), audience verification is skipped — so tokens work
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


class RegistryConfig(BaseModel):
    """Internal (mirrored) container registry."""

    url: str = "registry.internal"


class Settings(BaseSettings):
    """Application settings, loaded from env (prefix ``SERVERLESS_``) and ``.env``.

    Nested config uses a ``__`` delimiter (e.g. ``SERVERLESS_SSO__ISSUER``).
    """

    model_config = SettingsConfigDict(
        env_prefix="SERVERLESS_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    app_name: str = "serverless-api"
    port: int = 8080
    auth_enabled: bool = True
    base_domain: str = "example.com"
    # Single platform wildcard domain; host = {name}-{group}.{route_domain}
    route_domain: str = "serverless.example.com"

    # Browser origins allowed to call the API (e.g. the ServiceNow portal).
    # Empty disables CORS. env: SERVERLESS_CORS_ALLOW_ORIGINS (JSON list).
    cors_allow_origins: list[str] = Field(default_factory=list)

    client_cert_dir: str = "/etc/serverless/client"
    ca_bundle: CABundleConfig = Field(default_factory=CABundleConfig)

    # Per-call timeouts to a cluster's API server (seconds). Without these a down
    # cluster would block a worker thread until the OS socket timeout.
    cluster_connect_timeout: float = 2.0
    cluster_read_timeout: float = 5.0
    # Backstop for a whole per-site operation (covers several sequential calls).
    site_op_timeout: float = 60.0

    # Namespace (same in every cluster) where the API creates workloads.
    workloads_namespace: str = "serverless-workloads"

    sso: SSOConfig = Field(default_factory=SSOConfig)
    registry: RegistryConfig = Field(default_factory=RegistryConfig)
    sites: list[SiteConfig] = Field(default_factory=list)
    # The site this API instance runs in (active/active). Injected per-cluster
    # from the Helm `global.site` value (env SERVERLESS_LOCAL_SITE). Reads of
    # data that is uniform across sites (list, the spec/describe block) prefer
    # it; defaults to the first site when unset/unknown.
    local_site: str | None = None
    # Static admin API key for non-OIDC service accounts (opaque bearer token).
    # The RAW token, sourced from Vault via ESO (env SERVERLESS_ADMIN_API_KEY).
    # Matched with a constant-time compare (see app/auth/apikey.py). Falls back to
    # a well-known dev value when unset; set it empty ("") to disable API-key auth.
    admin_api_key: str = "Aa123456"

    @property
    def client_cert_file(self) -> str:
        """Absolute path to the client TLS certificate (``client_cert_dir/tls.crt``)."""
        return f"{self.client_cert_dir.rstrip('/')}/tls.crt"

    @property
    def client_key_file(self) -> str:
        """Absolute path to the client TLS key (``client_cert_dir/tls.key``)."""
        return f"{self.client_cert_dir.rstrip('/')}/tls.key"

    def site(self, name: str) -> SiteConfig | None:
        """Return the configured site with ``name``, or None if there isn't one.

        Args:
            name: The site name to look up.

        Returns:
            The matching :class:`SiteConfig`, or None.
        """
        return next((z for z in self.sites if z.name == name), None)

    @property
    def site_names(self) -> list[str]:
        """The names of all configured sites."""
        return [z.name for z in self.sites]


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
