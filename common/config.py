"""Settings shared by every service (api, builder, …).

The connection identity - sites, client cert, CA bundle, registry, timeouts - is
the same for any service that talks to the clusters, so it lives here. Each
service subclasses it and adds its own fields.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SiteConfig(BaseModel):
    """Connection profile for one site.

    A *site* is a region (e.g. ``central``, ``south``); it runs an OpenShift
    *cluster* (e.g. ``central-0``) whose API server is derived as
    ``https://api.{cluster}.{base_domain}:6443``. The client certificate, CA
    bundle, and workloads namespace are global (the same in every cluster).
    """

    name: str
    cluster: str


class CABundleConfig(BaseModel):
    """The OpenShift-injected trusted CA bundle ConfigMap.

    OpenShift populates a ConfigMap labelled
    ``config.openshift.io/inject-trusted-cabundle: "true"`` with the cluster's
    trusted CAs. It is mounted into the service and every workload, and is the same
    everywhere, so the Kubernetes client verifies API servers with it too.
    """

    config_map: str = "ca-bundle"
    key: str = "ca-bundle.crt"
    mount_path: str = "/etc/ssl/certs"

    @property
    def file(self) -> str:
        """Absolute path to the mounted CA bundle file (``mount_path/key``)."""
        return f"{self.mount_path.rstrip('/')}/{self.key}"


class RegistryConfig(BaseModel):
    """Internal (mirrored) container registry."""

    url: str = "registry.internal"
    organization: str = ""

    @property
    def base(self) -> str:
        """Registry host plus organization, the prefix every image ref hangs off."""
        url = self.url.strip("/")
        org = self.organization.strip("/")
        return f"{url}/{org}" if org else url


class BuildConfig(BaseModel):
    """kpack build settings (env ``SERVERLESS_BUILD__*``, set by the Helm chart)."""

    registry_secret: str = "serverless-registry-creds"  # noqa: S105 - a Secret name
    git_username: str = "x-access-token"  # noqa: S105 - a username, not a secret
    resources: dict = Field(default_factory=dict)


class CommonSettings(BaseSettings):
    """Settings any cluster-talking service needs, loaded from ``SERVERLESS_`` env.

    Nested config uses a ``__`` delimiter (e.g. ``SERVERLESS_CA_BUNDLE__KEY``).
    Subclass to add service-specific fields.
    """

    model_config = SettingsConfigDict(
        env_prefix="SERVERLESS_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    base_domain: str = "example.com"
    workloads_namespace: str = "serverless-workloads"

    client_cert_dir: str = "/etc/serverless/client"
    ca_bundle: CABundleConfig = Field(default_factory=CABundleConfig)
    registry: RegistryConfig = Field(default_factory=RegistryConfig)
    build: BuildConfig = Field(default_factory=BuildConfig)

    cluster_connect_timeout: float = 2.0
    cluster_read_timeout: float = 5.0
    site_op_timeout: float = 60.0

    sites: list[SiteConfig] = Field(default_factory=list)
    local_site: str | None = None

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
