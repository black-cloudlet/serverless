"""Settings shared by every service (api, builder, …).

The connection identity — sites, the client cert, the trusted CA bundle, the
internal registry, and per-cluster timeouts — is the same for any service that
talks to the clusters, so it lives here as :class:`CommonSettings`. Each service
subclasses it and adds its own fields (the API adds SSO, CORS, the route domain,
…). All settings load from the ``SERVERLESS_`` env prefix.
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

    name: str  # site/region, e.g. "central"
    cluster: str  # cluster instance name, e.g. "central-0"


class CABundleConfig(BaseModel):
    """The OpenShift-injected trusted CA bundle ConfigMap.

    OpenShift populates a ConfigMap labelled
    ``config.openshift.io/inject-trusted-cabundle: "true"`` with the cluster's
    trusted CAs. We mount it into the service and every workload; it is the same
    for every cluster, so the Kubernetes client also uses it to verify the API
    servers.
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
    # Organization/project path segment, when the registry namespaces its repos
    # (Harbor projects, Quay/GitLab orgs, Artifactory repo keys). Empty for a
    # flat registry.
    organization: str = ""

    @property
    def base(self) -> str:
        """Registry host plus organization, the prefix every image ref hangs off."""
        url = self.url.strip("/")
        org = self.organization.strip("/")
        return f"{url}/{org}" if org else url


class BuildConfig(BaseModel):
    """kpack build settings (env ``SERVERLESS_BUILD__*``, set by the Helm chart)."""

    # The shared registry credential every build pushes with, and every function
    # KSVC pulls with. Created by the chart from the ClusterSecretStore.
    registry_secret: str = "serverless-registry-creds"  # noqa: S105 - a Secret name
    # Username paired with the caller's git token in the basic-auth Secret kpack
    # consumes. GitHub and GitLab PATs accept any username; providers that check
    # it (Bitbucket app passwords) need this overridden.
    git_username: str = "x-access-token"  # noqa: S105 - a username, not a secret
    # Resource requests/limits for the build pod, applied to spec.build.resources.
    # A runtime may override with its own `buildResources` in the runtimes file.
    # env: SERVERLESS_BUILD__RESOURCES as a JSON object.
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
    # Namespace (same in every cluster) where workloads live.
    workloads_namespace: str = "serverless-workloads"

    client_cert_dir: str = "/etc/serverless/client"
    ca_bundle: CABundleConfig = Field(default_factory=CABundleConfig)
    registry: RegistryConfig = Field(default_factory=RegistryConfig)
    build: BuildConfig = Field(default_factory=BuildConfig)

    # Per-call timeouts to a cluster's API server (seconds). Without these a down
    # cluster would block a worker thread until the OS socket timeout.
    cluster_connect_timeout: float = 2.0
    cluster_read_timeout: float = 5.0
    # Backstop for a whole per-site operation (covers several sequential calls).
    site_op_timeout: float = 60.0

    sites: list[SiteConfig] = Field(default_factory=list)
    # The site this instance runs in (active/active). Injected per-cluster from
    # the Helm `global.site` value (env SERVERLESS_LOCAL_SITE); reads of data that
    # is uniform across sites prefer it, defaulting to the first site when unset.
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
