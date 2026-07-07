"""Per-site Kubernetes/OpenShift cluster client (server-side apply, get, delete).

Shared infrastructure: the API and a future builder service both talk to a
cluster the same way (client-cert mTLS, lazy connect). It is decoupled from any
one service's settings via :class:`ClusterConnection`, and generic over resource
kinds — each service supplies its own kind constants (any object exposing
``api_version`` and ``kind``), so this module carries no domain kinds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from kubernetes import client, utils
from kubernetes.dynamic import DynamicClient

from common.errors import NotFoundError


class Kind(Protocol):
    """A resource kind: its ``apiVersion`` and PascalCase ``kind``."""

    @property
    def api_version(self) -> str:
        """The resource ``apiVersion`` (e.g. ``serving.knative.dev/v1``)."""
        ...

    @property
    def kind(self) -> str:
        """The PascalCase ``kind`` (e.g. ``Service``)."""
        ...


@dataclass
class ClusterConnection:
    """Everything needed to reach one cluster, independent of any service config.

    Attributes:
        site: The site/region name (e.g. ``central``).
        name: The cluster instance name (e.g. ``central-0``).
        host: The API server URL (``https://…:6443``).
        namespace: The namespace operations target.
        ca_cert: Path to the CA bundle that verifies the API server.
        client_cert: Path to the client TLS certificate.
        client_key: Path to the client TLS key.
        connect_timeout: Per-call connect timeout (seconds).
        read_timeout: Per-call read timeout (seconds).
    """

    site: str
    name: str
    host: str
    namespace: str
    ca_cert: str
    client_cert: str
    client_key: str
    connect_timeout: float
    read_timeout: float


class Cluster:
    """A single site's cluster connection and resource operations.

    The Kubernetes client is synchronous and the connection is established lazily
    (on first use) so one unreachable site can't fail or block startup.
    """

    def __init__(self, conn: ClusterConnection):
        """Configure the client for one site (the connection stays lazy).

        Args:
            conn: The site's connection profile (endpoint, namespace, TLS,
                timeouts).
        """
        self.site: str = conn.site
        self.name: str = conn.name
        self._namespace: str = conn.namespace

        self._configuration = client.Configuration()
        self._configuration.host = conn.host
        self._configuration.ssl_ca_cert = conn.ca_cert
        self._configuration.cert_file = conn.client_cert
        self._configuration.key_file = conn.client_key

        self._api_client_obj: client.ApiClient | None = None
        self._dynamic_client_obj: DynamicClient | None = None
        self._opts: dict = {"_request_timeout": (conn.connect_timeout, conn.read_timeout)}

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

    def _dynamic_api(self, kind: Kind):
        """Resolve the dynamic resource API for a kind (apiVersion + kind)."""
        return self._dynamic_client.resources.get(kind.api_version, kind.kind)

    def connect(self) -> None:
        """Eagerly establish the connection (API discovery).

        So the first request doesn't pay for it. Idempotent - a no-op once
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
        self, kind: Kind, name: str | None = None, label_selector: str | None = None
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

    def delete(self, kind: Kind, name: str) -> None:
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

    def pod_logs(
        self,
        pod: str,
        *,
        container: str,
        since_seconds: int | None = None,
        limit_bytes: int | None = None,
    ) -> str:
        """Read a snapshot of one pod container's current log.

        Uses CoreV1Api directly — the dynamic client can't read the ``log``
        subresource. The returned text is whatever the node currently holds for
        the container (Kubernetes keeps no ring buffer beyond the node's rotated
        log file); it is not a live stream.

        Args:
            pod: The pod name.
            container: The container to read (e.g. the Knative user-container).
            since_seconds: Only return logs newer than this many seconds, if set.
            limit_bytes: Cap the number of bytes returned, if set.

        Returns:
            The log text.

        Raises:
            NotFoundError: If the pod is gone (404). Other errors propagate.
        """
        core = client.CoreV1Api(self._api_client)
        try:
            return core.read_namespaced_pod_log(
                name=pod,
                namespace=self._namespace,
                container=container,
                timestamps=True,
                since_seconds=since_seconds,
                limit_bytes=limit_bytes,
                **self._opts,
            )
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                raise NotFoundError(f"pod '{pod}' not found") from exc
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
