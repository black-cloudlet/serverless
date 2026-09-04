"""The registry of GVKs the platform operates on."""

from __future__ import annotations

from enum import Enum


class ResourceKind(Enum):
    """The resource kinds the platform manages, mapped to (apiVersion, kind)."""

    NAMESPACE = ("v1", "Namespace")
    NETWORK_POLICY = ("networking.k8s.io/v1", "NetworkPolicy")
    ROLE_BINDING = ("rbac.authorization.k8s.io/v1", "RoleBinding")
    KNATIVE_SERVICE = ("serving.knative.dev/v1", "Service")
    KNATIVE_REVISION = ("serving.knative.dev/v1", "Revision")
    DOMAIN_MAPPING = ("serving.knative.dev/v1beta1", "DomainMapping")
    CONFIG_MAP = ("v1", "ConfigMap")
    SECRET = ("v1", "Secret")
    SERVICE_ACCOUNT = ("v1", "ServiceAccount")
    POD = ("v1", "Pod")
    POD_METRICS = ("metrics.k8s.io/v1beta1", "PodMetrics")
    EXTERNAL_SECRET = ("external-secrets.io/v1beta1", "ExternalSecret")
    KPACK_IMAGE = ("kpack.io/v1alpha2", "Image")
    KPACK_BUILD = ("kpack.io/v1alpha2", "Build")
    # Trident Protect (docs/TENANT-CONTROLLER.md - Backups). Optional CRDs: a
    # cluster without it installed serves neither, which `Cluster.serves` answers.
    TRIDENT_APPLICATION = ("protect.trident.netapp.io/v1", "Application")
    TRIDENT_SCHEDULE = ("protect.trident.netapp.io/v1", "Schedule")

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
