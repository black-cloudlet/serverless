"""Settings shared by every service (api, builder, …).

The connection identity - regions, client cert, CA bundle, registry, timeouts - is
the same for any service that talks to the clusters, so it lives here. Each
service subclasses it and adds its own fields.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from common.errors import ValidationError
from common.names import NAMESPACE_SUFFIX, namespace_for_group


class RegionRegistry(BaseModel):
    """A region's own registry, overriding the platform default for that region.

    Part of the regions list, which is identical in every cluster: an instance
    composes the manifests for every region, not just its own. It carries no
    credentials - the list is serialized into a ConfigMap.

    Attributes:
        url: The registry host, with an optional port.
        organization: Overrides the platform's namespace segment. None inherits;
            "" overrides with nothing, which is how a registry says it has no
            namespacing path at all.
        repository: Overrides the segment everything the platform builds sits
            under, on the same None-inherits rule.
    """

    url: str
    organization: str | None = None
    repository: str | None = None


class RegionConfig(BaseModel):
    """Connection profile for one region.

    A *region* is a region (e.g. ``central``, ``south``); it runs an OpenShift
    *cluster* (e.g. ``central-0``) whose API server is derived as
    ``https://api.{cluster}.{base_domain}:6443``. The client certificate and CA
    bundle are global (the same in every cluster); the registry is not.

    Attributes:
        name: The region (region) name.
        cluster: The OpenShift cluster running it.
        registry: That region's registry, or None to take the platform default -
            which is what a single-registry install leaves it as.
    """

    name: str
    cluster: str
    registry: RegionRegistry | None = None


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


def registry_host(url: str) -> str:
    """A configured registry URL reduced to its canonical bare host.

    The one normalization every "same registry?" comparison must share:
    :attr:`RegistryConfig.host` (what image references and reclaim paths hang
    off) and :meth:`CommonSettings.registry_for`'s token fallback both reduce
    through here, so the same registry spelled with and without a scheme can
    never read as two different ones.

    Args:
        url: The configured registry URL, scheme and slashes tolerated.

    Returns:
        The bare host (and port, if any).
    """
    return url.strip("/").split("://", 1)[-1]


class RegistryConfig(BaseModel):
    """One internal (mirrored) container registry.

    Both the platform default and, once merged with a :class:`RegionRegistry` by
    :meth:`CommonSettings.registry_for`, one region's resolved registry: a caller
    holds a whole registry, never a base plus overrides still to apply
    (docs/RUNTIMES.md - Registry layout).
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
    def host(self) -> str:
        """The configured registry host, tolerant of a pasted scheme.

        The chart asks for a bare host; a scheme in the configured URL is
        stripped, so ``https://registry.internal`` and ``registry.internal``
        resolve to the same host.

        Also the test for "is this reference ours":
        :func:`common.registry.repository_path` matches references against this
        same host, so what the platform pushes and what it may reclaim agree on
        where the host ends and the repository begins.
        """
        return registry_host(self.url)

    @property
    def api_url(self) -> str:
        """Registry base URL. Always https - internal TLS is trusted via the CA bundle."""
        return f"https://{self.host}"

    @property
    def can_delete(self) -> bool:
        """Whether repository cleanup is both wanted and credentialed."""
        return bool(self.delete_on_function_delete and self.api_token)

    @property
    def path(self) -> str:
        """Everything between the host and a function's own ``{group}/{name}``.

        Two callers read exactly this string: the image reference hangs off it,
        and the repository *delete* addresses Quay by the same path with the
        host removed (docs/BUILDING.md - Registry cleanup on delete). Either
        part empty is skipped, so a flat ``{host}/{group}/{name}`` install still
        produces no leading or doubled slash.
        """
        parts = [p.strip("/") for p in (self.organization, self.repository) if p.strip("/")]
        return "/".join(parts)

    @property
    def base(self) -> str:
        """Registry host plus :attr:`path`, the prefix every image ref hangs off."""
        url = self.host
        return f"{url}/{self.path}" if self.path else url


class BuildConfig(BaseModel):
    """kpack build settings (env ``SERVERLESS_BUILD__*``, set by the Helm chart)."""

    registry_secret: str = "serverless-registry-creds"  # noqa: S105 - a Secret name
    # Pull-only credential for the shared kpack registry (stack, store, and the
    # run image `export` pulls). Empty when it is the region registry.
    kpack_registry_secret: str = ""  # noqa: S105 - a Secret name
    git_username: str = "x-access-token"  # noqa: S105 - a username, not a secret
    resources: dict = Field(default_factory=dict)
    # "registry" caches build layers in the registry the build already pushes to;
    # "inherit" writes no `spec.cache` and takes kpack's default, a PVC per Image.
    # docs/RUNTIMES.md - Build cache.
    cache: Literal["registry", "inherit"] = "registry"
    # Set explicitly: unset is kpack's default of 10 and 10, and each Build holds
    # a completed pod (docs/BUILDING.md - Build history).
    success_history_limit: int = Field(default=3, ge=1)
    failed_history_limit: int = Field(default=3, ge=1)


# The provision endpoint's per-region status vocabulary - the same words the
# API's RegionStatus uses, since provision rows render beside deploy rows.
# Shared here because both ends of the HTTP contract read them: the tenant
# controller reports these values and the API checks for READY.
PROVISION_READY = "Ready"
PROVISION_FAILED = "Failed"
PROVISION_TIMEOUT = "Timeout"


class TenantNamespaceConfig(BaseModel):
    """Where a group's workloads live, and how the API reaches the controller.

    One shared block read by both ends: the API derives the namespace it writes
    into from ``suffix``, and the tenant controller derives the one it creates
    from the same value.

    Attributes:
        suffix: Appended to the group. Must match the chart's
            ``tenantNamespaces.suffix``.
        controller_url: The tenant controller's in-cluster Service. Empty
            disables the provision call, which is the dev-cluster posture.
        token: Shared token presented to (and checked by) the provision
            endpoint. Empty disables the check; the NetworkPolicy is the
            primary control.
        timeout: Budget for one provision call, which a create waits on. It
            exceeds the controller's own converge budget (``cluster_op_timeout``,
            60s for the whole fan-out): a new group's first provision applies the
            full template set in every region, and giving up before that finishes
            turns the first create into a 503.
    """

    suffix: str = NAMESPACE_SUFFIX
    controller_url: str = ""
    token: str = ""
    timeout: float = 75.0

    def namespace_for(self, group: str) -> str:
        """The group's namespace under this config's suffix.

        The one adapter from the naming rule's ``ValueError`` to the
        API-facing 4xx, used by both ends so they reject the same groups with
        the same words.

        Args:
            group: The owning (normalized) group.

        Returns:
            ``{group}{suffix}``.

        Raises:
            ValidationError: If the group cannot name a namespace - too long
                with the suffix, or reserved.
        """
        try:
            return namespace_for_group(group, self.suffix)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc


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
    tenant_namespaces: TenantNamespaceConfig = Field(default_factory=TenantNamespaceConfig)

    client_cert_dir: str = "/etc/serverless/client"
    ca_bundle: CABundleConfig = Field(default_factory=CABundleConfig)
    # Platform default; anything about one region goes through `registry_for`.
    registry: RegistryConfig = Field(default_factory=RegistryConfig)
    # Registry API token per region name (SERVERLESS_REGION_REGISTRY_TOKENS, JSON).
    # Every instance holds every region's: a delete reclaims repositories in all
    # regions from whichever one took the request. Carried as one JSON object,
    # since a region name may contain '-' and env-var nesting cannot express it.
    region_registry_tokens: dict[str, str] = Field(default_factory=dict)
    build: BuildConfig = Field(default_factory=BuildConfig)

    cluster_connect_timeout: float = 2.0
    cluster_read_timeout: float = 5.0
    # Backstop on one cluster's whole operation inside a write fan-out (an apply
    # is several sequential calls). It bounds work against one cluster, whatever
    # region carries it.
    cluster_op_timeout: float = 60.0
    # The same backstop for the read fan-outs (list/get/stats). A cluster that
    # overruns it costs only its own column of the merged response
    # (docs/API.md - Partial-failure semantics).
    cluster_read_op_timeout: float = 5.0
    # The bounded pool the read fan-outs run on, and how much may queue for it.
    # It is separate from the process-wide default executor; past
    # workers + queued, the read is refused with 503.
    cluster_read_workers: int = 16
    cluster_read_max_queued: int = 32

    regions: list[RegionConfig] = Field(default_factory=list)
    local_region: str | None = None

    @property
    def client_cert_file(self) -> str:
        """Absolute path to the client TLS certificate (``client_cert_dir/tls.crt``)."""
        return f"{self.client_cert_dir.rstrip('/')}/tls.crt"

    @property
    def client_key_file(self) -> str:
        """Absolute path to the client TLS key (``client_cert_dir/tls.key``)."""
        return f"{self.client_cert_dir.rstrip('/')}/tls.key"

    def region(self, name: str) -> RegionConfig | None:
        """Return the configured region with ``name``, or None if there isn't one.

        Args:
            name: The region name to look up.

        Returns:
            The matching :class:`RegionConfig`, or None.
        """
        return next((z for z in self.regions if z.name == name), None)

    def registry_for(self, region: str) -> RegistryConfig:
        """The registry ``region`` builds into, pulls from, and is cleaned up in.

        The single derivation, so what a build pushes to, what its KSVC pulls,
        and what a delete reclaims cannot disagree. A region with no override -
        the normal single-registry install - resolves to the platform default.

        Args:
            region: The region name.

        Returns:
            A new registry: the platform default with that region's overrides and
            API token applied. Nothing here is mutated.
        """
        profile = self.region(region)
        override = profile.registry if profile else None
        # A region with no token of its own falls back to the platform token,
        # and only when its registry is on the SAME host: the default token
        # belongs to the default registry and is never sent to another one.
        token = self.region_registry_tokens.get(region) or ""
        # Compared as canonical hosts, the same normalization the references and
        # reclaim paths use, so one registry spelled with and without a scheme
        # is one host.
        same_host = override is None or registry_host(override.url) == registry_host(
            self.registry.url
        )
        if not token and same_host:
            token = self.registry.api_token
        update: dict[str, object] = {"api_token": token}
        if override is not None:
            update["url"] = override.url
            # None inherits, "" overrides with nothing - see RegionRegistry.
            if override.organization is not None:
                update["organization"] = override.organization
            if override.repository is not None:
                update["repository"] = override.repository
        return self.registry.model_copy(update=update)

    @property
    def region_names(self) -> list[str]:
        """The names of all configured regions."""
        return [z.name for z in self.regions]


class LoopSettings(CommonSettings):
    """Settings for a paced control loop (``common.loop``).

    Read by the build controller and the tenant controller, which pass these
    values to :func:`common.loop.run_loop`.
    """

    # The pass cadence: how long the controller's watch is held open, and the
    # pause between the tenant controller's reconcile passes.
    resync_seconds: int = Field(default=300, gt=0)
    # Only for a pass that raised; a clean pass follows the resync cadence.
    error_backoff_seconds: float = Field(default=5.0, ge=0)
