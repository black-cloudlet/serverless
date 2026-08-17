"""Unit tests for the per-region Cluster client's resource resolution."""

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


def _requests_through(cluster) -> list:
    """Capture the timeout urllib3 actually receives for a request on ``cluster``.

    Asserted at ``urlopen``, below everything the kubernetes client does to a
    ``_request_timeout``, because the bug this guards against lives exactly
    there: ``connection_pool_kw["timeout"]`` looks like it sets a default and
    does not, since urllib3 honours the pool default only for its own sentinel
    and ``rest.py`` always passes ``timeout=`` explicitly.
    """
    seen = []
    pool_manager = cluster._api_client.rest_client.pool_manager

    class _Response:
        status = 200
        reason = "OK"
        data = b"{}"

    def urlopen(method, url, **kwargs):
        seen.append(kwargs.get("timeout"))
        return _Response()

    pool_manager.urlopen = urlopen
    return seen


def test_a_call_with_no_per_request_timeout_still_carries_a_connect_timeout():
    """Discovery and the watch pass no per-request timeout, and are exactly the
    calls that must not wedge their thread forever against a cluster that
    blackholes connections. Connect only - a read bound would tear down an idle
    watch or log follow between bytes."""
    from common.cluster import Cluster
    from common.config import CommonSettings, RegionConfig

    settings = CommonSettings(
        regions=[RegionConfig(name="central", cluster="central-0")],
        cluster_connect_timeout=1.5,
    )
    cluster = Cluster(settings.regions[0], settings)
    seen = _requests_through(cluster)

    cluster._api_client.rest_client.request("GET", "https://api.central-0.example.com:6443/api")

    assert [(t.connect_timeout, t.read_timeout) for t in seen] == [(1.5, None)]
    cluster.close()


def test_a_call_that_names_its_own_timeout_keeps_it():
    """The default fills a gap; it must not override the per-request read
    timeout ordinary calls carry, nor the log follow's connect-only pair."""
    from common.cluster import Cluster
    from common.config import CommonSettings, RegionConfig

    settings = CommonSettings(
        regions=[RegionConfig(name="central", cluster="central-0")],
        cluster_connect_timeout=1.5,
        cluster_read_timeout=7.0,
    )
    cluster = Cluster(settings.regions[0], settings)
    seen = _requests_through(cluster)

    cluster._api_client.rest_client.request(
        "GET",
        "https://api.central-0.example.com:6443/api",
        _request_timeout=(0.5, 7.0),
    )

    assert [(t.connect_timeout, t.read_timeout) for t in seen] == [(0.5, 7.0)]
    cluster.close()


def test_the_connection_pool_keeps_urllib3s_own_socket_options():
    """Replacing the defaults instead of adding to them drops TCP_NODELAY, which
    re-enables Nagle on every cluster connection - a delayed-ACK stall per call
    and per streamed chunk."""
    from urllib3.connection import HTTPConnection

    from common.cluster import Cluster
    from common.config import CommonSettings, RegionConfig

    settings = CommonSettings(regions=[RegionConfig(name="central", cluster="central-0")])
    cluster = Cluster(settings.regions[0], settings)
    options = cluster._api_client.rest_client.pool_manager.connection_pool_kw["socket_options"]

    assert set(HTTPConnection.default_socket_options) <= set(options)
    cluster.close()


def test_the_connection_pool_enables_tcp_keepalive():
    """The streams that deliberately carry no read timeout (watch, log follow)
    have no other defence against a silently dead connection: the server-side
    timeout cannot arrive over dead TCP, and the blocked thread is the build
    controller's whole reconcile loop."""
    import socket as socket_mod

    from common.cluster import Cluster
    from common.config import CommonSettings, RegionConfig

    settings = CommonSettings(regions=[RegionConfig(name="central", cluster="central-0")])
    cluster = Cluster(settings.regions[0], settings)
    options = cluster._api_client.rest_client.pool_manager.connection_pool_kw["socket_options"]

    assert (socket_mod.SOL_SOCKET, socket_mod.SO_KEEPALIVE, 1) in options
    cluster.close()
