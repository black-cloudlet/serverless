"""Settings shared by every service (api, builder, …).

The connection identity - sites, client cert, CA bundle, registry, timeouts - is
the same for any service that talks to the clusters, so it lives here. Each
service subclasses it and adds its own fields.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SiteRegistry(BaseModel):
    """A site's own registry, overriding the platform default for that site.

    Every site builds into and pulls from its own registry, so a site that only
    *runs* a function still reads from storage it owns (docs/PER-SITE-REGISTRY.md).
    Which registry that is has to be known by every instance, not just the one
    sitting in that site: the API composes the manifests for all sites, so it
    needs the peer's registry to write the peer's ``Image`` and KSVC. That is why
    this lives in the sites list - identical in every cluster - rather than
    beside the per-release ``local_site``.

    It carries **no credentials**, deliberately: the sites list is serialized
    into a ConfigMap. What a site needs to authenticate arrives separately - the
    push credential as a Secret in that site's own cluster, the registry API
    token through :attr:`CommonSettings.site_registry_tokens`.

    Attributes:
        url: The registry host (with an optional port). Required - a site
            override that does not move the host is not an override.
        organization: Overrides the platform's namespace segment. None inherits;
            "" does not. The distinction is load-bearing, since "" is how an
            install says a registry has no namespacing path at all, and a site
            has to be able to say that too.
        repository: Overrides the segment everything the platform builds sits
            under, on the same None-inherits/""-overrides rule.
    """

    url: str
    organization: str | None = None
    repository: str | None = None


class SiteConfig(BaseModel):
    """Connection profile for one site.

    A *site* is a region (e.g. ``central``, ``south``); it runs an OpenShift
    *cluster* (e.g. ``central-0``) whose API server is derived as
    ``https://api.{cluster}.{base_domain}:6443``. The client certificate, CA
    bundle, and workloads namespace are global (the same in every cluster);
    the registry is not.

    Attributes:
        name: The site (region) name.
        cluster: The OpenShift cluster running it.
        registry: That site's registry, or None to take the platform default -
            which is what a single-registry install leaves it as.
    """

    name: str
    cluster: str
    registry: SiteRegistry | None = None


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
    """One internal (mirrored) container registry.

    The platform default, and - once merged with a :class:`SiteRegistry` by
    :meth:`CommonSettings.registry_for` - the resolved registry of one site.
    Both are the same type on purpose: everything downstream wants a whole
    registry (``base`` to build a reference, ``api_url``/``can_delete`` to
    reclaim a repository), not a base plus a pile of overrides to re-apply.
    """

    url: str = "registry.internal"
    organization: str = ""
    # Path segment between the organization and a function's own {group}/{name},
    # from the chart's `build.builderRepository` - the same prefix the Builder
    # images sit under, so everything the platform pushes shares one root.
    repository: str = ""
    # Quay OAuth token used to delete a deleted function's repositories. Not the
    # push robot - robots cannot call the management API. Absent, cleanup is
    # skipped entirely. docs/BUILDING.md - Registry cleanup on delete.
    api_token: str = ""
    delete_on_function_delete: bool = True
    timeout: float = 10.0

    @property
    def api_url(self) -> str:
        """Registry base URL. Always https - internal TLS is trusted via the CA bundle."""
        return f"https://{self.url.strip('/')}"

    @property
    def can_delete(self) -> bool:
        """Whether repository cleanup is both wanted and credentialed."""
        return bool(self.delete_on_function_delete and self.api_token)

    @property
    def path(self) -> str:
        """Everything between the host and a function's own ``{group}/{name}``.

        Its own property because two callers need exactly this string and must
        not derive it separately: the image reference hangs off it, and the
        repository *delete* addresses Quay by the same path with the host
        removed (docs/BUILDING.md - Registry cleanup on delete). Either part
        empty is skipped, so a flat ``{host}/{group}/{name}`` install still
        produces no leading or doubled slash.
        """
        parts = [p.strip("/") for p in (self.organization, self.repository) if p.strip("/")]
        return "/".join(parts)

    @property
    def base(self) -> str:
        """Registry host plus :attr:`path`, the prefix every image ref hangs off."""
        url = self.url.strip("/")
        return f"{url}/{self.path}" if self.path else url


class BuildConfig(BaseModel):
    """kpack build settings (env ``SERVERLESS_BUILD__*``, set by the Helm chart)."""

    registry_secret: str = "serverless-registry-creds"  # noqa: S105 - a Secret name
    git_username: str = "x-access-token"  # noqa: S105 - a username, not a secret
    resources: dict = Field(default_factory=dict)
    # "registry" caches build layers in the registry the build already pushes to;
    # "inherit" writes no `spec.cache` and takes kpack's default, a PVC per Image.
    # docs/BUILDING.md - Build cache.
    cache: Literal["registry", "inherit"] = "registry"
    # Set explicitly: unset is kpack's default of 10 and 10, and each Build holds
    # a completed pod (docs/BUILDING.md - Build history).
    success_history_limit: int = Field(default=3, ge=1)
    failed_history_limit: int = Field(default=3, ge=1)


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
    # The platform default. A site may override the host and the path segments
    # (SiteConfig.registry); resolve the pair with `registry_for`, never by
    # reading this directly on a path that is about one site.
    registry: RegistryConfig = Field(default_factory=RegistryConfig)
    # Registry API token per site, keyed by site name, from
    # SERVERLESS_SITE_REGISTRY_TOKENS as a JSON object. Every instance needs
    # EVERY site's token, not just its own: a delete reclaims the function's
    # repositories in all sites from whichever instance took the request. A site
    # this does not name falls back to `registry.api_token`, which is what a
    # single-registry install keeps using.
    #
    # A JSON object rather than one variable per site, because a site name is a
    # DNS-1123 label and may contain '-', which is not portable in an
    # environment variable name. Spelled with one underscore, so it cannot be
    # confused with SERVERLESS_REGISTRY__API_TOKEN, which is the fallback above.
    site_registry_tokens: dict[str, str] = Field(default_factory=dict)
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

    def registry_for(self, site: str) -> RegistryConfig:
        """The registry ``site`` builds into, pulls from, and is cleaned up in.

        The one place a site's registry is derived, so the image reference a
        build pushes to, the reference its KSVC pulls, and the repository a
        delete reclaims cannot disagree about which registry they mean - the
        same reason :attr:`RegistryConfig.path` is a single property.

        An unknown site, or one with no override, resolves to the platform
        default. That is not a fallback for a typo so much as the normal state
        of a single-registry install, where no site names a registry at all.

        Args:
            site: The site name.

        Returns:
            The platform default with that site's overrides and API token
            applied. A new object each call; neither this settings object nor
            the default registry is mutated.
        """
        profile = self.site(site)
        override = profile.registry if profile else None
        # Falls back rather than defaulting to "": an empty token disables
        # cleanup (`can_delete`), so a site absent from the map would silently
        # stop reclaiming repositories instead of using the platform token.
        update: dict[str, object] = {
            "api_token": self.site_registry_tokens.get(site) or self.registry.api_token
        }
        if override is not None:
            update["url"] = override.url
            # None inherits, "" overrides with nothing - see SiteRegistry.
            if override.organization is not None:
                update["organization"] = override.organization
            if override.repository is not None:
                update["repository"] = override.repository
        return self.registry.model_copy(update=update)

    @property
    def site_names(self) -> list[str]:
        """The names of all configured sites."""
        return [z.name for z in self.sites]
