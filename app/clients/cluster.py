"""``Cluster`` — the single runtime object per OpenShift cluster.

One instance represents one site/cluster. It holds that site's :class:`SiteConfig`
and pulls the shared bits (client cert, CA bundle, timeouts, workloads namespace)
from :class:`Settings`, so the rest of the app works with ``Cluster`` only — never
``SiteConfig`` directly. It fully encapsulates the ``kubernetes`` library: nobody
outside this module imports ``kubernetes`` or passes raw apiVersion/kind strings —
callers use the :class:`ResourceKind` enum.

Authentication uses the global cert-manager client certificate over mTLS
(CN = serverless-api.clients.{base_domain}); the same identity/CA is valid in
every cluster (docs §6.3).
"""

from __future__ import annotations

from enum import Enum

from app.core.config import Settings, SiteConfig
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
    """One OpenShift cluster. Holds its SiteConfig; reads shared bits from Settings."""

    FIELD_MANAGER = "serverless-api"

    def __init__(self, config: SiteConfig, settings: Settings):
        self._config = config
        self._settings = settings
        self._dynamic = None

    # -- identity / config ------------------------------------------------
    @property
    def name(self) -> str:  # site/region name
        return self._config.name

    @property
    def cluster(self) -> str | None:  # cluster instance name, e.g. central-0
        return self._config.cluster

    @property
    def api_server(self) -> str:
        return self._config.api_server

    @property
    def namespace(self) -> str:  # workloads namespace (global)
        return self._settings.workloads_namespace

    # -- connection -------------------------------------------------------
    def _client(self):
        if self._dynamic is not None:
            return self._dynamic

        from kubernetes import client
        from kubernetes.dynamic import DynamicClient

        cfg = client.Configuration()
        cfg.host = self._config.api_server
        cfg.ssl_ca_cert = self._settings.ca_bundle.path
        cfg.cert_file = self._settings.client_cert_path
        cfg.key_file = self._settings.client_key_path
        api = client.ApiClient(cfg)

        self._dynamic = DynamicClient(api)
        return self._dynamic

    def _resource(self, api_version: str, kind: str):
        return self._client().resources.get(api_version=api_version, kind=kind)

    def _ns(self, namespace: str | None) -> str:
        return namespace or self.namespace

    def _opts(self) -> dict:
        # (connect, read) timeout on every call so an unreachable cluster fails
        # fast instead of blocking the worker thread.
        return {
            "_request_timeout": (
                self._settings.cluster_connect_timeout,
                self._settings.cluster_read_timeout,
            )
        }

    # -- operations -------------------------------------------------------
    def apply(self, manifest: dict, namespace: str | None = None) -> dict:
        """Idempotent create-or-update via Kubernetes server-side apply."""
        res = self._resource(manifest["apiVersion"], manifest["kind"])
        return self._client().server_side_apply(
            res,
            body=manifest,
            name=manifest["metadata"]["name"],
            namespace=self._ns(namespace),
            field_manager=self.FIELD_MANAGER,
            force_conflicts=True,
            **self._opts(),
        ).to_dict()

    def get(self, kind: ResourceKind, name: str, namespace: str | None = None) -> dict:
        return (
            self._resource(kind.api_version, kind.kind)
            .get(name=name, namespace=self._ns(namespace), **self._opts())
            .to_dict()
        )

    def list(
        self,
        kind: ResourceKind,
        namespace: str | None = None,
        label_selector: str | None = None,
    ) -> list[dict]:
        result = self._resource(kind.api_version, kind.kind).get(
            namespace=self._ns(namespace), label_selector=label_selector, **self._opts()
        )
        return [i.to_dict() for i in result.items]

    def delete(
        self, kind: ResourceKind, name: str, namespace: str | None = None
    ) -> None:
        self._resource(kind.api_version, kind.kind).delete(
            name=name, namespace=self._ns(namespace), **self._opts()
        )
