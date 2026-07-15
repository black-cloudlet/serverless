"""Unit tests for the per-site Cluster client's resource resolution."""

from common.cluster import Cluster, ResourceKind


class _FakeResources:
    """Stands in for the dynamic client's resource discoverer.

    Its ``get`` takes **keyword-only** arguments, mirroring the real
    ``Discoverer.get(self, **kwargs)`` - so a positional call (the old bug) would
    raise ``TypeError`` here too.
    """

    def __init__(self):
        self.calls = []

    def get(self, **kwargs):
        self.calls.append(kwargs)
        return f"api::{kwargs['api_version']}::{kwargs['kind']}"


class _FakeDynamicClient:
    def __init__(self):
        self.resources = _FakeResources()


def _cluster_with(dynamic):
    """A Cluster with its lazy dynamic client pre-injected (no real connection)."""
    cluster = object.__new__(Cluster)  # bypass __init__ (no TLS/config needed)
    cluster._dynamic_client_obj = dynamic
    return cluster


def test_dynamic_api_resolves_by_keyword_gvk():
    # Regression: resources.get() is keyword-only (api_version=, kind=); calling it
    # positionally raised "TypeError: get() takes 1 positional argument but 3 given".
    dynamic = _FakeDynamicClient()
    cluster = _cluster_with(dynamic)

    result = cluster._dynamic_api(ResourceKind.KNATIVE_SERVICE)

    assert dynamic.resources.calls == [
        {"api_version": "serving.knative.dev/v1", "kind": "Service"}
    ]
    assert result == "api::serving.knative.dev/v1::Service"


def test_dynamic_api_passes_each_kinds_gvk():
    dynamic = _FakeDynamicClient()
    cluster = _cluster_with(dynamic)

    for kind in (ResourceKind.SECRET, ResourceKind.POD, ResourceKind.POD_METRICS):
        cluster._dynamic_api(kind)

    assert dynamic.resources.calls == [
        {"api_version": "v1", "kind": "Secret"},
        {"api_version": "v1", "kind": "Pod"},
        {"api_version": "metrics.k8s.io/v1beta1", "kind": "PodMetrics"},
    ]
