"""One namespace curried over a cluster - the ergonomic half of the client.

:class:`~common.cluster.client.Cluster` is cluster-scoped; code that works
within a single namespace binds it once and passes this view around.
"""

from __future__ import annotations

from collections.abc import Iterator

from common.cluster.follow import LogFollow
from common.cluster.kinds import ResourceKind
from common.config import RegistryConfig


class NamespacedCluster:
    """A :class:`Cluster` with one namespace curried into every operation.

    Bound once where the namespace is decided, so downstream code cannot mix
    namespaces mid-operation. The view owns no connection - hence no
    ``close``; closing is the owner's call. Duck-typed, so a test's fake
    cluster can stand in underneath.
    """

    def __init__(self, cluster, namespace: str):
        """Bind one namespace over one cluster.

        Args:
            cluster: The underlying Cluster (or a test double of one).
            namespace: The namespace every operation targets.
        """
        self.cluster = cluster
        self.namespace = namespace

    @property
    def region(self) -> str:
        """The underlying cluster's region name."""
        return self.cluster.region

    @property
    def name(self) -> str:
        """The underlying cluster's cluster name."""
        return self.cluster.name

    @property
    def registry(self) -> RegistryConfig:
        """The underlying cluster's registry."""
        return self.cluster.registry

    def connect(self) -> None:
        """Eagerly establish the underlying connection (see Cluster.connect)."""
        self.cluster.connect()

    def apply(self, manifest: dict, *, field_manager: str | None = None) -> list[dict]:
        """Server-side apply into the bound namespace (see Cluster.apply)."""
        # Forwarded only when set, so fakes need the parameter only if used.
        extra: dict = {"field_manager": field_manager} if field_manager else {}
        return self.cluster.apply(manifest, namespace=self.namespace, **extra)

    def get(
        self, kind: ResourceKind, name: str | None = None, label_selector: str | None = None
    ) -> dict | list[dict]:
        """Get or list within the bound namespace (see Cluster.get)."""
        return self.cluster.get(kind, name, label_selector, namespace=self.namespace)

    def list_resources(
        self, kind: ResourceKind, *, label_selector: str | None = None
    ) -> tuple[list[dict], str | None]:
        """List within the bound namespace (see Cluster.list_resources)."""
        return self.cluster.list_resources(
            kind, namespace=self.namespace, label_selector=label_selector
        )

    def watch(
        self,
        kind: ResourceKind,
        *,
        resource_version: str | None = None,
        label_selector: str | None = None,
        timeout_seconds: int | None = None,
    ) -> Iterator[tuple[str, dict]]:
        """Watch within the bound namespace (see Cluster.watch)."""
        return self.cluster.watch(
            kind,
            namespace=self.namespace,
            resource_version=resource_version,
            label_selector=label_selector,
            timeout_seconds=timeout_seconds,
        )

    def patch(self, kind: ResourceKind, name: str, body: dict) -> dict:
        """Merge-patch within the bound namespace (see Cluster.patch)."""
        return self.cluster.patch(kind, name, body, namespace=self.namespace)

    def delete(self, kind: ResourceKind, name: str) -> None:
        """Delete within the bound namespace (see Cluster.delete)."""
        self.cluster.delete(kind, name, namespace=self.namespace)

    def pod_logs(
        self,
        pod: str,
        *,
        container: str,
        since_seconds: int | None = None,
        limit_bytes: int | None = None,
        tail_lines: int | None = None,
    ) -> str:
        """Read a pod-log snapshot in the bound namespace (see Cluster.pod_logs)."""
        return self.cluster.pod_logs(
            pod,
            namespace=self.namespace,
            container=container,
            since_seconds=since_seconds,
            limit_bytes=limit_bytes,
            tail_lines=tail_lines,
        )

    def follow_pod_logs(
        self,
        pod: str,
        *,
        container: str,
        since_seconds: int | None = None,
        tail_lines: int | None = None,
    ) -> "LogFollow":
        """Open a pod-log follow in the bound namespace (see Cluster.follow_pod_logs)."""
        return self.cluster.follow_pod_logs(
            pod,
            namespace=self.namespace,
            container=container,
            since_seconds=since_seconds,
            tail_lines=tail_lines,
        )
