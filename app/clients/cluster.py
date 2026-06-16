"""``Cluster`` — the single wrapper around the Kubernetes/OpenShift client.

One instance represents one site (one OpenShift cluster). It fully encapsulates
the ``kubernetes`` library: nobody outside this module imports ``kubernetes`` or
passes raw apiVersion/kind strings — callers use the :class:`ResourceKind` enum.

Authentication uses the site's cert-manager-issued client certificate
(CN = serverless-api.clients.{base_domain}) over mTLS, or the in-cluster service
account for the API's local cluster (docs §6.3).
"""

from __future__ import annotations

from enum import Enum

from app.core.config import SiteConfig
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
    """A connection to one OpenShift cluster, scoped to its workloads namespace.

    Always authenticates with the global cert-manager client certificate over
    mTLS (the same identity/CA is valid in every cluster).
    """

    def __init__(
        self,
        config: SiteConfig,
        *,
        client_cert_path: str,
        client_key_path: str,
        ca_path: str,
        request_timeout: tuple[float, float] | None = None,
    ):
        self._config = config
        self._client_cert_path = client_cert_path
        self._client_key_path = client_key_path
        self._ca_path = ca_path
        # (connect, read) seconds, passed to every API call so an unreachable
        # cluster fails fast instead of blocking the worker thread.
        self._timeout = request_timeout
        self._dynamic = None

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def namespace(self) -> str:
        return self._config.namespace

    # -- connection -------------------------------------------------------
    def _client(self):
        if self._dynamic is not None:
            return self._dynamic

        from kubernetes import client
        from kubernetes.dynamic import DynamicClient

        cfg = client.Configuration()
        cfg.host = self._config.api_server
        cfg.ssl_ca_cert = self._ca_path
        cfg.cert_file = self._client_cert_path
        cfg.key_file = self._client_key_path
        api = client.ApiClient(cfg)

        self._dynamic = DynamicClient(api)
        return self._dynamic

    def _resource(self, api_version: str, kind: str):
        return self._client().resources.get(api_version=api_version, kind=kind)

    def _ns(self, namespace: str | None) -> str:
        return namespace or self.namespace

    def _opts(self) -> dict:
        return {"_request_timeout": self._timeout} if self._timeout else {}

    # -- operations -------------------------------------------------------
    def apply(self, manifest: dict, namespace: str | None = None) -> dict:
        """Create the object, or update it if it already exists (idempotent)."""
        from kubernetes.dynamic.exceptions import NotFoundError

        ns = self._ns(namespace)
        opts = self._opts()
        res = self._resource(manifest["apiVersion"], manifest["kind"])
        name = manifest["metadata"]["name"]
        try:
            res.get(name=name, namespace=ns, **opts)
            return res.patch(
                body=manifest,
                namespace=ns,
                content_type="application/merge-patch+json",
                **opts,
            ).to_dict()
        except NotFoundError:
            return res.create(body=manifest, namespace=ns, **opts).to_dict()

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
