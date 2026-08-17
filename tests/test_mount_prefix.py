"""The API served under a mount prefix.

There is one path per endpoint and it is the complete one. These tests pin that:
what the API publishes is what answers, and nothing answers beside it. The rest
of the suite runs with no prefix configured, where the complete path is just
``/v1/...``.
"""

from __future__ import annotations

import pytest
from cloudlet_apis.auth import Principal, StreamTickets
from fastapi.testclient import TestClient

from api.auth.deps import get_tickets, optional_auth, require_auth
from api.core.config import get_settings
from api.core.paths import api_base
from api.dependencies import get_container_service, get_function_service
from api.main import create_app

# The stubs the other suites already build; this file varies the prefix, not them.
from tests.test_auth_and_deployer import _FakeCluster, _workload_service
from tests.test_stream_endpoints import FakeStreams

PREFIX = "/api/serverless"
BASE = f"{PREFIX}/v1"
KEY = "mount-prefix-test-signing-key-0123"  # noqa: S105 - a fixture, not a credential
CALLER = Principal(subject="u", username="alice", groups=["team"], is_admin=False)
STREAM = f"{BASE}/groups/team/functions/foo/pods"


@pytest.fixture
def prefixed(monkeypatch):
    """Settings with the API mounted under PREFIX, cleared again afterwards."""
    get_settings.cache_clear()
    monkeypatch.setenv("SERVERLESS_EXTERNAL_BASE_PATH", PREFIX)
    yield
    get_settings.cache_clear()


def _client():
    """A client with the header auth stubbed and the ticket half left real."""
    app = create_app()
    svc = FakeStreams(events=[])
    app.dependency_overrides[require_auth] = lambda: CALLER
    app.dependency_overrides[optional_auth] = lambda: CALLER
    app.dependency_overrides[get_function_service] = lambda: svc
    app.dependency_overrides[get_container_service] = lambda: svc
    app.dependency_overrides[get_tickets] = lambda: StreamTickets(KEY)
    return TestClient(app)


# --- the setting ------------------------------------------------------------


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("/api/serverless", "/api/serverless/v1"),
        ("/api/serverless/", "/api/serverless/v1"),  # trailing slash is not a difference
        ("/", "/v1"),  # a root mount is what empty already means
        ("", "/v1"),
    ],
)
def test_the_base_path_is_the_prefix_plus_the_version(monkeypatch, configured, expected):
    get_settings.cache_clear()
    monkeypatch.setenv("SERVERLESS_EXTERNAL_BASE_PATH", configured)
    try:
        assert api_base() == expected
    finally:
        get_settings.cache_clear()


def test_a_prefix_without_a_leading_slash_is_refused(monkeypatch):
    """It would be concatenated into 'api/serverless/v1/...', which routes nowhere."""
    get_settings.cache_clear()
    monkeypatch.setenv("SERVERLESS_EXTERNAL_BASE_PATH", "api/serverless")
    try:
        with pytest.raises(ValueError, match="must start with"):
            get_settings()
    finally:
        get_settings.cache_clear()


# --- one path in ------------------------------------------------------------


def test_the_complete_path_is_the_only_way_in(prefixed):
    client = _client()

    assert client.get(f"{BASE}/functions/info").status_code == 200
    # The version alone is not an address: without the prefix nothing answers.
    assert client.get("/v1/functions/info").status_code == 404


def test_the_probes_stay_off_the_prefix(prefixed):
    """The kubelet reaches the pod directly, not through whatever serves the API."""
    client = _client()

    assert client.get("/healthz").status_code == 200
    assert client.get(f"{PREFIX}/healthz").status_code == 404


# --- what the app hands a client -------------------------------------------


async def test_status_url_is_the_complete_path(prefixed):
    """A 202's poll target is called by the client, so it is the whole path.

    Through the real service: a stub returning its own ``statusUrl`` would pass
    whatever this asserted.
    """
    from fastapi import BackgroundTasks

    from api.models.container import ContainerCreate
    from api.services.container import ContainerService

    engine = _workload_service({"region-a": _FakeCluster("region-a")})
    spec = ContainerCreate(
        name="app", image="reg/x:1", port=8080, registryUsername="u", registryToken="t"
    )

    body = await ContainerService(engine).accept("team", spec, CALLER, BackgroundTasks())

    assert body.statusUrl == f"{BASE}/groups/team/containers/app"


def test_the_openapi_document_is_served_and_addressed_under_the_prefix(prefixed):
    client = _client()

    assert client.get("/openapi.json").status_code == 404
    schema = client.get(f"{PREFIX}/openapi.json").json()

    # The paths carry the prefix because they *are* the paths - there is no
    # second, shorter spelling for a `servers` entry to make up the difference.
    assert f"{BASE}/groups/{{group}}/functions" in schema["paths"]
    assert "servers" not in schema


@pytest.mark.parametrize("page", ["/docs", "/redoc"])
def test_the_docs_publish_urls_that_answer(prefixed, page):
    """Asserting the HTML alone would pass while every asset 404'd."""
    client = _client()

    html = client.get(f"{PREFIX}{page}").text
    assert f"{PREFIX}/openapi.json" in html
    assert f"{PREFIX}/static/" in html

    assert client.get(f"{PREFIX}/static/swagger-ui.css").status_code == 200
    # Nothing at the root, where another API on the same host would answer.
    assert client.get(page).status_code == 404
    assert client.get("/static/swagger-ui.css").status_code == 404


# --- the stream tickets -----------------------------------------------------


def test_a_ticket_is_minted_and_spent_on_the_same_path(prefixed):
    """Mint and verify hold one string, so there is nothing to keep in step."""
    client = _client()

    minted = client.post(f"{BASE}/stream-tickets", json={"path": STREAM}).json()

    assert minted["path"] == STREAM
    assert client.get(f"{STREAM}?ticket={minted['ticket']}").status_code == 200


def test_a_path_without_the_prefix_is_not_mintable(prefixed):
    """It is not a path this API serves, so a ticket for it would open nothing."""
    client = _client()

    response = client.post(
        f"{BASE}/stream-tickets", json={"path": "/v1/groups/team/functions/foo/pods"}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_a_ticket_is_still_bound_to_one_stream(prefixed):
    client = _client()

    minted = client.post(f"{BASE}/stream-tickets", json={"path": STREAM}).json()
    other = f"{BASE}/groups/team/functions/foo/stats/stream"

    assert client.get(f"{other}?ticket={minted['ticket']}").status_code == 401


# --- the compatibility setting the chart ships ------------------------------


@pytest.fixture
def as_before(monkeypatch):
    """The chart's default: the /api/v1/... surface this API served before."""
    get_settings.cache_clear()
    monkeypatch.setenv("SERVERLESS_EXTERNAL_BASE_PATH", "/api")
    yield
    get_settings.cache_clear()


def test_the_old_paths_are_what_the_chart_default_serves(as_before):
    """``externalBasePath: /api`` is what makes the base-path move invisible.

    Every existing client calls ``/api/v1/...``. If this breaks, the chart's
    default is no longer a safe upgrade.
    """
    client = _client()

    assert client.get("/api/v1/functions/info").status_code == 200
    assert client.get("/api/v1/containers/info").status_code == 200
    assert client.get("/api/docs").status_code == 200


async def test_the_old_status_url_is_what_a_client_gets_back(as_before):
    from fastapi import BackgroundTasks

    from api.models.container import ContainerCreate
    from api.services.container import ContainerService

    engine = _workload_service({"region-a": _FakeCluster("region-a")})
    spec = ContainerCreate(
        name="app", image="reg/x:1", port=8080, registryUsername="u", registryToken="t"
    )

    body = await ContainerService(engine).accept("team", spec, CALLER, BackgroundTasks())

    assert body.statusUrl == "/api/v1/groups/team/containers/app"


def test_an_old_browser_stream_still_authenticates(as_before):
    client = _client()
    old_stream = "/api/v1/groups/team/functions/foo/pods"

    minted = client.post("/api/v1/stream-tickets", json={"path": old_stream}).json()

    assert minted["path"] == old_stream
    assert client.get(f"{old_stream}?ticket={minted['ticket']}").status_code == 200
