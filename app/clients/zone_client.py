"""Per-zone Kubernetes/OpenShift client.

Authenticates to a cluster with its cert-manager-issued client certificate
(CN = serverless-api.clients.{base_domain}) over mTLS, or with the in-cluster
service account for the API's local cluster. Wraps the dynamic client so we can
manage CRDs (Knative Service, DomainMapping, OpenShift Route) uniformly.
"""

from __future__ import annotations

from app.core.config import ZoneConfig
from app.core.logging import get_logger

logger = get_logger(__name__)


class ZoneClient:
    def __init__(self, zone: ZoneConfig):
        self.zone = zone
        self._dynamic = None

    # -- connection -------------------------------------------------------
    def _client(self):
        if self._dynamic is not None:
            return self._dynamic

        from kubernetes import client, config as kube_config
        from kubernetes.dynamic import DynamicClient

        if self.zone.in_cluster:
            kube_config.load_incluster_config()
            api = client.ApiClient()
        else:
            cfg = client.Configuration()
            cfg.host = self.zone.api_server
            cfg.ssl_ca_cert = self.zone.ca_path
            cfg.cert_file = self.zone.client_cert_path
            cfg.key_file = self.zone.client_key_path
            api = client.ApiClient(cfg)

        self._dynamic = DynamicClient(api)
        return self._dynamic

    def _resource(self, api_version: str, kind: str):
        return self._client().resources.get(api_version=api_version, kind=kind)

    # -- operations -------------------------------------------------------
    def apply(self, manifest: dict, namespace: str) -> dict:
        """Create the object, or update it if it already exists (idempotent)."""
        from kubernetes.dynamic.exceptions import NotFoundError

        res = self._resource(manifest["apiVersion"], manifest["kind"])
        name = manifest["metadata"]["name"]
        try:
            res.get(name=name, namespace=namespace)
            return res.patch(
                body=manifest,
                namespace=namespace,
                content_type="application/merge-patch+json",
            ).to_dict()
        except NotFoundError:
            return res.create(body=manifest, namespace=namespace).to_dict()

    def get(self, api_version: str, kind: str, name: str, namespace: str) -> dict:
        return self._resource(api_version, kind).get(
            name=name, namespace=namespace
        ).to_dict()

    def list(
        self,
        api_version: str,
        kind: str,
        namespace: str,
        label_selector: str | None = None,
    ) -> list[dict]:
        result = self._resource(api_version, kind).get(
            namespace=namespace, label_selector=label_selector
        )
        return [i.to_dict() for i in result.items]

    def delete(self, api_version: str, kind: str, name: str, namespace: str) -> None:
        self._resource(api_version, kind).delete(name=name, namespace=namespace)
