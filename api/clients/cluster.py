"""The resource kinds the API manages (GVKs for the shared cluster client).

The generic cluster client lives in :mod:`common.cluster`; this module supplies
the API's own kind constants (Knative + core objects). A builder service would
define its own (Tekton) kinds the same way.
"""

from __future__ import annotations

from enum import Enum


class ResourceKind(Enum):
    """The resource kinds the API manages, mapped to (apiVersion, kind)."""

    KNATIVE_SERVICE = ("serving.knative.dev/v1", "Service")
    KNATIVE_REVISION = ("serving.knative.dev/v1", "Revision")
    DOMAIN_MAPPING = ("serving.knative.dev/v1beta1", "DomainMapping")
    CONFIG_MAP = ("v1", "ConfigMap")
    SECRET = ("v1", "Secret")
    POD = ("v1", "Pod")
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
