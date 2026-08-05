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

    assert dynamic.resources.calls == [{"api_version": "serving.knative.dev/v1", "kind": "Service"}]
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


class _Item:
    """Stands in for a ResourceInstance - the dynamic client hands back objects."""

    def __init__(self, obj):
        self._obj = obj

    def to_dict(self):
        return self._obj


class _Listing:
    def __init__(self, items, resource_version):
        self.items = [_Item(o) for o in items]
        self.metadata = type("Meta", (), {"resourceVersion": resource_version})


class _WatchableApi:
    """A dynamic resource API recording the list/watch arguments it was called with."""

    def __init__(self, listing=None, events=()):
        self.listing = listing
        self.events = events
        self.get_kwargs = None
        self.watch_kwargs = None

    def get(self, **kwargs):
        self.get_kwargs = kwargs
        return self.listing

    def watch(self, **kwargs):
        self.watch_kwargs = kwargs
        return iter(self.events)


def _cluster_calling(api):
    """A Cluster whose _dynamic_api always resolves to `api`."""
    cluster = object.__new__(Cluster)
    cluster._namespace = "serverless-workloads"
    cluster._opts = {"_request_timeout": (2.0, 5.0)}
    cluster._dynamic_api = lambda kind: api
    return cluster


def test_list_resources_returns_the_objects_and_the_watch_position():
    # The resourceVersion is what makes the listing resumable: a watch started
    # from it replays everything since, so relist-then-follow has no gap.
    api = _WatchableApi(_Listing([{"metadata": {"name": "fn-hello"}}], "4242"))

    objects, version = _cluster_calling(api).list_resources(
        ResourceKind.KPACK_IMAGE, label_selector="a=b"
    )

    assert objects == [{"metadata": {"name": "fn-hello"}}]
    assert version == "4242"
    assert api.get_kwargs["label_selector"] == "a=b"
    assert api.get_kwargs["namespace"] == "serverless-workloads"


def test_list_resources_tolerates_a_server_that_reports_no_version():
    api = _WatchableApi(_Listing([], None))

    assert _cluster_calling(api).list_resources(ResourceKind.KPACK_IMAGE) == ([], None)


def test_watch_yields_typed_events_and_carries_the_resume_position():
    api = _WatchableApi(events=[{"type": "MODIFIED", "object": _Item({"kind": "Image"})}])

    events = list(
        _cluster_calling(api).watch(
            ResourceKind.KPACK_IMAGE,
            resource_version="7",
            label_selector="a=b",
            timeout_seconds=300,
        )
    )

    assert events == [("MODIFIED", {"kind": "Image"})]
    assert api.watch_kwargs == {
        "namespace": "serverless-workloads",
        "resource_version": "7",
        "label_selector": "a=b",
        "timeout": 300,
    }


def test_watch_does_not_impose_the_per_request_read_timeout():
    # A watch is idle between events by design; the read timeout would tear it down.
    api = _WatchableApi(events=[])

    list(_cluster_calling(api).watch(ResourceKind.KPACK_IMAGE))

    assert "_request_timeout" not in api.watch_kwargs
