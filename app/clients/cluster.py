"""Per-site Kubernetes/OpenShift cluster client (server-side apply, get, delete)."""

from __future__ import annotations

from enum import Enum

from kubernetes import client, utils
from kubernetes.dynamic import DynamicClient

from app.core.config import Settings, SiteConfig
from app.core.errors import NotFoundError


class ResourceKind(Enum):
    """The resource kinds the API manages, mapped to (apiVersion, kind)."""

    KNATIVE_SERVICE = ("serving.knative.dev/v1", "Service")
    KNATIVE_REVISION = ("serving.knative.dev/v1", "Revision")
    DOMAIN_MAPPING = ("serving.knative.dev/v1beta1", "DomainMapping")
    CONFIG_MAP = ("v1", "ConfigMap")
    SECRET = ("v1", "Secret")
    POD_METRICS = ("metrics.k8s.io/v1beta1", "PodMetrics")

    @property
    def api_version(self) -> str:
        """The resource's ``apiVersion`` (e.g. ``serving.knative.dev/v1``)."""
        return self.value[0]

    @property
    def kind(self) -> str:
        """The resource's PascalCase ``kind`` (e.g. ``Service``)."""
        return self.value[1]

    @classmethod
    def from_kind(cls, kind: str) -> "ResourceKind":
        """Map a manifest's ``kind`` string (e.g. "Secret") to its ResourceKind.

        Args:
            kind: The PascalCase kind string.

        Returns:
            The matching ResourceKind.

        Raises:
            ValueError: If no member has that kind.
        """
        for member in cls:
            if member.kind == kind:
                return member
        raise ValueError(f"unknown resource kind: {kind!r}")


class Cluster:
    """A single site's cluster connection and resource operations.

    The Kubernetes client is synchronous and the connection is established lazily
    (on first use) so one unreachable site can't fail or block API startup.
    """

    def __init__(self, site_config: SiteConfig, settings: Settings):
        """Configure the client for one site (the connection stays lazy).

        Args:
            site_config: The site's name and cluster identifiers.
            settings: Global settings (namespace, TLS material, timeouts).
        """
        self.site: str = site_config.name
        self.name: str = site_config.cluster
        self._namespace: str = settings.workloads_namespace

        self._configuration = client.Configuration()
        self._configuration.host = f"https://api.{self.name}.{settings.base_domain}:6443"

        self._configuration.ssl_ca_cert = settings.ca_bundle.file
        self._configuration.cert_file = settings.client_cert_file
        self._configuration.key_file = settings.client_key_file

        self._api_client_obj: client.ApiClient | None = None
        self._dynamic_client_obj: DynamicClient | None = None
        self._opts: dict = {
            "_request_timeout": (
                settings.cluster_connect_timeout,
                settings.cluster_read_timeout,
            )
        }

    @property
    def _api_client(self) -> client.ApiClient:
        """The lazily-built Kubernetes API client for this site."""
        if self._api_client_obj is None:
            self._api_client_obj = client.ApiClient(self._configuration)
        return self._api_client_obj

    @property
    def _dynamic_client(self) -> DynamicClient:
        """The lazily-built dynamic client (does API discovery on first use)."""
        if self._dynamic_client_obj is None:
            self._dynamic_client_obj = DynamicClient(self._api_client)
        return self._dynamic_client_obj

    def _dynamic_api(self, kind: ResourceKind):
        """Resolve the dynamic resource API for a ResourceKind (apiVersion + kind)."""
        return self._dynamic_client.resources.get(kind.api_version, kind.kind)

    def connect(self) -> None:
        """Eagerly establish the connection (API discovery).

        So the first request doesn't pay for it. Idempotent — a no-op once
        connected. Blocking.
        """
        _ = self._dynamic_client

    def apply(self, manifest: dict) -> list[dict]:
        """Server-side apply a manifest (create-or-update), forcing conflicts.

        Args:
            manifest: The resource manifest dict to apply.

        Returns:
            The applied object(s) as dicts (including server-assigned fields).
        """
        results = utils.create_from_dict(
            self._api_client,
            manifest,
            verbose=False,
            namespace=self._namespace,
            apply=True,
            force_conflicts=True,
            **self._opts,
        )
        return [i.to_dict() for i in results]

    def get(
        self, kind: ResourceKind, name: str | None = None, label_selector: str | None = None
    ) -> dict | list[dict]:
        """Get a resource by name, or list a kind by label selector.

        Args:
            kind: The resource kind to fetch.
            name: The object name for a single get; None to list.
            label_selector: Label selector for the list form.

        Returns:
            The object dict (named get) or a list of object dicts (list form).

        Raises:
            NotFoundError: If a named get returns a 404. Other errors propagate.
        """
        dynamic_api = self._dynamic_api(kind)
        if name is None:
            results = dynamic_api.get(
                namespace=self._namespace, label_selector=label_selector, **self._opts
            )
            return [i.to_dict() for i in results.items]
        try:
            return dynamic_api.get(name=name, namespace=self._namespace, **self._opts).to_dict()
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                raise NotFoundError(f"{kind.kind} '{name}' not found") from exc
            raise

    def delete(self, kind: ResourceKind, name: str) -> None:
        """Delete a resource by name.

        Args:
            kind: The resource kind to delete.
            name: The object name.

        Raises:
            NotFoundError: If the resource is already absent (404). Other errors
                propagate, so callers can tell "already gone" from a real failure.
        """
        dynamic_api = self._dynamic_api(kind)
        try:
            dynamic_api.delete(name=name, namespace=self._namespace, **self._opts)
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                raise NotFoundError(f"{kind.kind} '{name}' not found") from exc
            raise

    def close(self) -> None:
        """Release the underlying HTTP client (connection pool) for this site.

        Idempotent and safe to call at shutdown; the lazy clients are rebuilt on
        next use if the Cluster is reused afterwards.
        """
        if self._api_client_obj is not None:
            self._api_client_obj.close()
            self._api_client_obj = None
        self._dynamic_client_obj = None
