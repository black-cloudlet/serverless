"""The ensure fan-out and the tenant controller's internal HTTP surface.

Ensure is the one tenant-controller path that writes to every region, and the one a
create blocks on, so what matters here is that a single region's trouble stays
a *row* rather than an exception that loses the regions that worked - and that
the endpoint's verdicts are the ones the API is written against: 400 for a
group that cannot name a namespace, 401 for a bad token, 502 only when nothing
landed anywhere.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from common.labels import (
    ANNOTATION_TEMPLATE_HASH,
    LABEL_GROUP,
    LABEL_MANAGED_BY,
    TENANT_CONTROLLER_VALUE,
)
from tenant_controller import api as controller_api
from tenant_controller.api import create_app
from tenant_controller.config import TenantControllerSettings
from tenant_controller.ensure import FAILED, READY, TIMEOUT, ensure
from tenant_controller.templates import TemplateSet

TEMPLATES = {
    "10-namespace.yaml": """\
apiVersion: v1
kind: Namespace
metadata:
  name: {{namespace}}
""",
    "20-policy.yaml": """\
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny
  namespace: {{namespace}}
spec:
  podSelector: {}
""",
}


class _Cluster:
    """A cluster-scoped fake recording what converge wrote to it."""

    name = "cluster-0"

    def __init__(
        self, region: str, *, fail: str | None = None, block: threading.Event | None = None
    ):
        self.region = region
        # Resolved per region on the real client, which is what makes a
        # per-region value in the set follow the cluster being written to.
        self.registry = SimpleNamespace(url=f"registry.{region}.internal")
        self.applied: list[tuple[dict, str | None]] = []
        self._fail = fail
        self._block = block

    def apply(self, manifest, *, namespace=None, field_manager=None):
        if self._block is not None:
            self._block.wait(timeout=10)
        if self._fail:
            raise RuntimeError(self._fail)
        self.applied.append((manifest, namespace))
        return [manifest]

    def get(self, kind, name=None, label_selector=None, *, namespace):
        return []  # nothing left over, so the prune deletes nothing

    def delete(self, kind, name, *, namespace):
        raise AssertionError("nothing should be pruned in these fixtures")

    @property
    def namespaces(self) -> list[dict]:
        """The Namespace manifests this cluster was handed, in order."""
        return [m for m, _ in self.applied if m["kind"] == "Namespace"]


@pytest.fixture
def pool():
    """The app-owned pool ensure runs its converges on."""
    with ThreadPoolExecutor(max_workers=4) as executor:
        yield executor


def _templates() -> TemplateSet:
    return TemplateSet.from_sources(TEMPLATES.items())


def _settings(tmp_path, **overrides) -> TenantControllerSettings:
    for name, text in TEMPLATES.items():
        (tmp_path / name).write_text(text)
    return TenantControllerSettings(templates_dir=str(tmp_path), **overrides)


def _endpoints(app) -> dict:
    """``{path: (handler, methods)}``, reaching through the wrapper FastAPI puts
    around an included router (its shape has changed across versions)."""
    found, stack = {}, list(app.routes)
    while stack:
        route = stack.pop()
        inner = getattr(route, "original_router", None)
        if inner is not None:
            stack.extend(inner.routes)
        elif isinstance(route, APIRoute):
            found[route.path] = (route.endpoint, set(route.methods or ()))
    return found


def _client(clusters, settings) -> TestClient:
    # raise_server_exceptions=False, so a handler bug surfaces as the 500 a
    # caller would see rather than as an exception the test never posted.
    return TestClient(create_app(clusters, settings), raise_server_exceptions=False)


async def test_ensure_converges_every_region(pool):
    clusters = [_Cluster("central"), _Cluster("south")]
    templates = _templates()

    outcomes = await ensure(
        clusters, "payments-serverless", "payments", templates, timeout=10, executor=pool
    )

    assert [(o.region, o.status) for o in outcomes] == [("central", READY), ("south", READY)]
    for cluster in clusters:
        # The stamp protocol in both: opened without the hash, closed with it.
        first, last = cluster.namespaces[0], cluster.namespaces[-1]
        assert ANNOTATION_TEMPLATE_HASH not in (first["metadata"].get("annotations") or {})
        assert last["metadata"]["annotations"][ANNOTATION_TEMPLATE_HASH] == templates.digest
        assert last["metadata"]["name"] == "payments-serverless"
        assert last["metadata"]["labels"][LABEL_GROUP] == "payments"
        assert last["metadata"]["labels"][LABEL_MANAGED_BY] == TENANT_CONTROLLER_VALUE


async def test_a_failing_region_is_a_row_and_the_others_still_converge(pool):
    clusters = [_Cluster("central", fail="apiserver said no"), _Cluster("south")]

    outcomes = await ensure(
        clusters, "payments-serverless", "payments", _templates(), timeout=10, executor=pool
    )

    assert [(o.region, o.status) for o in outcomes] == [("central", FAILED), ("south", READY)]
    assert "apiserver said no" in outcomes[0].message
    assert outcomes[1].message is None
    assert clusters[1].namespaces, "the healthy region still got its converge"


async def test_regions_past_the_deadline_are_reported_as_timeout(pool):
    blocked = threading.Event()
    clusters = [_Cluster("central", block=blocked), _Cluster("south", block=blocked)]
    try:
        started = time.monotonic()
        outcomes = await ensure(
            clusters, "payments-serverless", "payments", _templates(), timeout=0.3, executor=pool
        )
        elapsed = time.monotonic() - started
    finally:
        blocked.set()  # release the abandoned converges before the suite moves on

    assert [o.status for o in outcomes] == [TIMEOUT, TIMEOUT]
    assert "timed out after 0.3s" in outcomes[0].message
    # One wait for the whole fan-out, so two blocked regions cost one deadline
    # rather than one each - and neither costs the 10s the fake would block for.
    assert elapsed < 2.0


async def test_ensure_refuses_an_empty_region_list(pool):
    with pytest.raises(ValueError, match="no regions are configured"):
        await ensure([], "payments-serverless", "payments", _templates(), timeout=10, executor=pool)


def test_the_endpoint_reports_the_namespace_the_hash_and_every_region(tmp_path):
    clusters = [_Cluster("central"), _Cluster("south")]
    settings = _settings(tmp_path)

    response = _client(clusters, settings).put("/groups/payments/namespace")

    assert response.status_code == 200
    body = response.json()
    assert body["group"] == "payments"
    assert body["namespace"] == "payments-serverless"
    assert body["templateHash"] == TemplateSet.load(settings.templates_dir).digest
    assert body["regions"] == [
        {"region": "central", "status": READY, "message": None},
        {"region": "south", "status": READY, "message": None},
    ]


def test_the_endpoint_normalizes_the_group_before_deriving_the_namespace(tmp_path):
    clusters = [_Cluster("central")]

    response = _client(clusters, _settings(tmp_path)).put("/groups/ggd-1234-My_Team/namespace")

    assert response.status_code == 200
    assert response.json()["namespace"] == "my-team-serverless"


def test_a_second_ensure_is_a_no_op_beyond_re_applying(tmp_path):
    clusters = [_Cluster("central")]
    client = _client(clusters, _settings(tmp_path))

    first = client.put("/groups/payments/namespace").json()
    writes = len(clusters[0].applied)
    second = client.put("/groups/payments/namespace").json()

    assert first == second, "ensure is idempotent: same namespace, same hash, same rows"
    assert len(clusters[0].applied) == 2 * writes, "and it re-applies rather than skipping"


@pytest.mark.parametrize(
    "group",
    [
        pytest.param("kube-team", id="reads as a system namespace"),
        pytest.param("a" * 60, id="too long once suffixed"),
    ],
)
def test_a_group_that_cannot_name_a_namespace_is_refused_before_any_write(tmp_path, group):
    clusters = [_Cluster("central")]

    response = _client(clusters, _settings(tmp_path)).put(f"/groups/{group}/namespace")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert clusters[0].applied == []


def test_the_endpoint_fails_the_call_only_when_no_region_landed(tmp_path):
    clusters = [_Cluster("central", fail="down"), _Cluster("south", fail="down")]

    response = _client(clusters, _settings(tmp_path)).put("/groups/payments/namespace")

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "REGION_TOTAL_FAILURE"
    assert [row["region"] for row in error["details"]] == ["central", "south"]


def test_one_region_landing_is_still_a_success(tmp_path):
    clusters = [_Cluster("central", fail="down"), _Cluster("south")]

    response = _client(clusters, _settings(tmp_path)).put("/groups/payments/namespace")

    assert response.status_code == 200
    assert [row["status"] for row in response.json()["regions"]] == [FAILED, READY]


@pytest.mark.parametrize(
    "header",
    [
        pytest.param({}, id="no header"),
        pytest.param({"Authorization": "Bearer wrong"}, id="wrong token"),
        pytest.param({"Authorization": "s3cret"}, id="no bearer scheme"),
    ],
)
def test_a_configured_token_is_required(tmp_path, header):
    clusters = [_Cluster("central")]
    settings = _settings(tmp_path, tenant_namespaces={"token": "s3cret"})

    response = _client(clusters, settings).put("/groups/payments/namespace", headers=header)

    assert response.status_code == 401
    assert clusters[0].applied == []


def test_the_configured_token_admits_the_caller(tmp_path):
    clusters = [_Cluster("central")]
    settings = _settings(tmp_path, tenant_namespaces={"token": "s3cret"})

    response = _client(clusters, settings).put(
        "/groups/payments/namespace", headers={"Authorization": "Bearer s3cret"}
    )

    assert response.status_code == 200


def test_no_configured_token_leaves_the_endpoint_open(tmp_path):
    clusters = [_Cluster("central")]

    response = _client(clusters, _settings(tmp_path)).put("/groups/payments/namespace")

    assert response.status_code == 200


def test_liveness_is_constant_and_readiness_names_the_loaded_set(tmp_path):
    settings = _settings(tmp_path)
    client = _client([_Cluster("central")], settings)

    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["templateHash"] == TemplateSet.load(settings.templates_dir).digest


@pytest.mark.parametrize(
    "prepare, reason",
    [
        pytest.param(lambda p: p / "missing", "a broken mount", id="directory absent"),
        pytest.param(lambda p: p, "an empty ConfigMap", id="directory empty"),
    ],
)
def test_readiness_fails_on_an_unusable_template_set(tmp_path, prepare, reason):
    settings = TenantControllerSettings(templates_dir=str(prepare(tmp_path)))

    response = _client([_Cluster("central")], settings).get("/readyz")

    assert response.status_code == 503, reason


@pytest.mark.parametrize("path", ["/readyz", "/groups/payments/namespace"])
def test_a_tenant_controller_with_no_regions_is_unavailable_not_broken(tmp_path, path):
    """Its own misconfiguration, so 503 - not a 500, and not the caller's fault."""
    client = _client([], _settings(tmp_path))

    response = client.get(path) if path == "/readyz" else client.put(path)

    assert response.status_code == 503
    assert "no regions are configured" in response.json()["error"]["message"]


def test_an_unusable_template_set_fails_ensure_the_way_it_fails_readiness(tmp_path):
    """One gate for both, so readiness can never say ready on a set ensure refuses."""
    settings = TenantControllerSettings(templates_dir=str(tmp_path / "missing"))
    client = _client([_Cluster("central")], settings)

    assert client.get("/readyz").status_code == 503
    assert client.put("/groups/payments/namespace").status_code == 503


def test_readiness_rejects_a_set_that_renders_only_a_namespace(tmp_path):
    """The rule converge refuses on. Unchecked here, the pod stays in rotation
    and every ensure 502s, blaming the regions for this pod's own bad mount."""
    (tmp_path / "10-namespace.yaml").write_text(TEMPLATES["10-namespace.yaml"])
    settings = TenantControllerSettings(templates_dir=str(tmp_path))
    client = _client([_Cluster("central")], settings)

    ready = client.get("/readyz")
    assert ready.status_code == 503
    assert "renders no namespaced contents" in ready.json()["error"]["message"]
    # And the endpoint answers for the same reason, rather than as a 502.
    assert client.put("/groups/payments/namespace").status_code == 503


def test_no_endpoint_holds_one_of_the_servers_threads(tmp_path):
    """All three are coroutines, so none consumes a slot of Starlette's thread
    limiter - which an ensure would hold for the whole cluster op timeout,
    starving the probes that keep this pod alive."""
    app = create_app([_Cluster("central")], _settings(tmp_path))

    handlers = _endpoints(app)
    assert set(handlers) == {"/healthz", "/readyz", "/groups/{group}/namespace"}
    for path, (handler, _methods) in handlers.items():
        assert inspect.iscoroutinefunction(handler), f"{path} would take a server thread"


def test_converging_a_group_is_a_put():
    """The method carries the contract: the API calls this before every create
    and retries it on timeout, so a generic client must know it repeats safely."""
    settings = TenantControllerSettings()
    methods = _endpoints(create_app([_Cluster("central")], settings))
    assert methods["/groups/{group}/namespace"][1] == {"PUT"}


async def test_the_probes_answer_while_an_ensure_is_in_flight(tmp_path):
    blocked = threading.Event()
    app = create_app([_Cluster("central", block=blocked)], _settings(tmp_path))
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://tenant-controller"
        ) as client:
            in_flight = asyncio.ensure_future(client.put("/groups/payments/namespace"))
            await asyncio.sleep(0.05)  # let it reach the blocked converge
            assert (await client.get("/healthz")).status_code == 200
            assert (await client.get("/readyz")).status_code == 200
            blocked.set()
            assert (await in_flight).status_code == 200
    finally:
        blocked.set()


def test_the_app_drains_its_pool_before_the_process_lets_go(tmp_path, monkeypatch):
    """Ordering that matters: main.py closes the cluster clients after this, so
    an in-flight converge must not lose the connection it writes through."""
    built = {}

    class _RecordingPool(ThreadPoolExecutor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            built["pool"] = self
            built["max_workers"] = kwargs.get("max_workers")

        def shutdown(self, wait=True, *, cancel_futures=False):
            built["shutdown"] = {"wait": wait, "cancel_futures": cancel_futures}
            super().shutdown(wait=wait, cancel_futures=cancel_futures)

    monkeypatch.setattr(controller_api, "ThreadPoolExecutor", _RecordingPool)
    settings = _settings(tmp_path, ensure_workers=3)

    # Entering TestClient runs startup; leaving it runs shutdown.
    with TestClient(create_app([_Cluster("central")], settings)) as client:
        assert client.put("/groups/payments/namespace").status_code == 200
        assert "shutdown" not in built, "the pool must live as long as the app"

    assert built["max_workers"] == 3, "bounded by ensure_workers, not the default"
    assert built["shutdown"] == {"wait": True, "cancel_futures": True}


def test_readiness_never_touches_a_cluster(tmp_path):
    """A probe that read a cluster would take the pod out on someone else's outage."""

    class _Unreachable:
        region = "central"

        def __getattr__(self, name):
            raise AssertionError(f"readiness called cluster.{name}")

    response = _client([_Unreachable()], _settings(tmp_path)).get("/readyz")

    assert response.status_code == 200
