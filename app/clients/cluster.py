from __future__ import annotations

from enum import Enum
from kubernetes import client, utils
from kubernetes.dynamic import DynamicClient, resource.Resource

from app.core.config import SiteConfig, Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

class ResourceKind(Enum):
    """The resource kinds the API manages, mapped to (apiVersion, kind)."""

    KNATIVE_SERVICE = ("serving.knative.dev/v1", "Service")
    DOMAIN_MAPPING = ("serving.knative.dev/v1beta1", "DomainMapping")
    CONFIG_MAP = ("v1", "ConfigMap")
    SECRET = ("v1", "Secret")

    @property
    def api_version(self) -> str:
        return self.value[0]

    @property
    def kind(self) -> str:
        return self.value[1]


class Cluster:
    def __init__(self, site_config: SiteConfig, settings: Settings):
        self.site: str = site_config.name
        self.name: str = site_config.cluster
        self._namespace: str = settings.workloads_namespace

        configuration = client.Configuration()
        configuration.host = f"api.{self.name}.{settings.base_domain}:6443"

        configuration.ssl_ca_cert = settings.ca_bundle.file
        configuration.cert_file = settings.client_cert_file
        configuration.key_file = settings.client_key_file

        self._api_client = client.ApiClient(configuration)
        self._dynamic_client = DynamicClient(self._api_client)
        self._opts: dict = {
            "_request_timeout": (
                settings.cluster_connect_timeout,
                settings.cluster_read_timeout,
            )
        }

    def _dynamic_api(kind: ResourceKind):
        return self._dynamic_client.resources.get(kind.api_version, kind.kind)


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


    def get(self, kind: ResourceKind, name: str | None = None, label_selector: str | None = None) -> dict | list[dict]:
        dynamic_api = self._dynamic_api(kind)
        if name is not None:
            return dynamic_api.get(name=name, namespace=self._namespace, **self._opts).to_dict()

        results = dynamic_api.get(namespace=self._namespace, label_selector=label_selector, **self._opts)
        return [i.to_dict() for i in results.items]

    def delete(self, kind: ResourceKind, name: str) -> None:
        dynamic_api = self._dynamic_api(kind)
        dynamic_api.delete(name=name, namespace=self._namespace, **self._opts)
