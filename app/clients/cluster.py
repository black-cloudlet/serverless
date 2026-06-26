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
        return self.value[0]

    @property
    def kind(self) -> str:
        return self.value[1]

    @classmethod
    def from_kind(cls, kind: str) -> "ResourceKind":
        """Map a manifest's ``kind`` string (e.g. "Secret") to its ResourceKind."""
        for member in cls:
            if member.kind == kind:
                return member
        raise ValueError(f"unknown resource kind: {kind!r}")


class Cluster:
    def __init__(self, site_config: SiteConfig, settings: Settings):
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
        if self._api_client_obj is None:
            self._api_client_obj = client.ApiClient(self._configuration)
        return self._api_client_obj

    @property
    def _dynamic_client(self) -> DynamicClient:
        if self._dynamic_client_obj is None:
            self._dynamic_client_obj = DynamicClient(self._api_client)
        return self._dynamic_client_obj

    def _dynamic_api(self, kind: ResourceKind):
        return self._dynamic_client.resources.get(kind.api_version, kind.kind)

    def connect(self) -> None:
        """Eagerly establish the connection (API discovery) so the first request
        doesn't pay for it. Idempotent — a no-op once connected. Blocking."""
        _ = self._dynamic_client

    def apply(self, manifest: dict) -> list[dict]:
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
        dynamic_api = self._dynamic_api(kind)
        dynamic_api.delete(name=name, namespace=self._namespace, **self._opts)
