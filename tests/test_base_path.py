"""The API served under a base path.

There is one path per endpoint and it is the complete one. These tests pin that:
what the API publishes is what answers, and nothing answers beside it. The rest
of the suite runs with no base path configured, where the complete path is just
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

# The stubs the other suites already build; this file varies the base path, not them.
from tests.test_auth_and_deployer import _FakeCluster, _workload_service
from tests.test_stream_endpoints import FakeStreams

BASE_PATH = "/api/serverless"
BASE = f"{BASE_PATH}/v1"
KEY = "base-path-test-signing-key-01234567"  # noqa: S105 - a fixture, not a credential
CALLER = Principal(subject="u", username="alice", groups=["team"], is_admin=False)
STREAM = f"{BASE}/groups/team/functions/foo/pods"


@pytest.fixture
def served_under(monkeypatch):
    """Settings with the API served under BASE_PATH, cleared again afterwards."""
    get_settings.cache_clear()
    monkeypatch.setenv("SERVERLESS_BASE_PATH", BASE_PATH)
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
        ("/", "/v1"),  # serving at the root is what empty already means
        ("", "/v1"),
    ],
)
def test_the_api_base_is_the_base_path_plus_the_version(monkeypatch, configured, expected):
    get_settings.cache_clear()
    monkeypatch.setenv("SERVERLESS_BASE_PATH", configured)
    try:
        assert api_base() == expected
    finally:
        get_settings.cache_clear()


def test_a_base_path_without_a_leading_slash_is_refused(monkeypatch):
    """It would be concatenated into 'api/serverless/v1/...', which routes nowhere."""
    get_settings.cache_clear()
    monkeypatch.setenv("SERVERLESS_BASE_PATH", "api/serverless")
    try:
        with pytest.raises(ValueError, match="must start with"):
            get_settings()
    finally:
        get_settings.cache_clear()


# --- one path in ------------------------------------------------------------


def test_the_complete_path_is_the_only_way_in(served_under):
    client = _client()

    assert client.get(f"{BASE}/functions/info").status_code == 200
    # The version alone is not an address: without the base path nothing answers.
    assert client.get("/v1/functions/info").status_code == 404


def test_the_probes_stay_outside_the_base_path(served_under):
    """The kubelet reaches the pod directly, not through whatever serves the API."""
    client = _client()

    assert client.get("/healthz").status_code == 200
    assert client.get(f"{BASE_PATH}/healthz").status_code == 404


# --- what the app hands a client -------------------------------------------


async def test_status_url_is_the_complete_path(served_under):
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


def test_the_openapi_document_is_served_and_addressed_under_the_base_path(served_under):
    client = _client()

    assert client.get("/openapi.json").status_code == 404
    schema = client.get(f"{BASE_PATH}/openapi.json").json()

    # The paths carry the base path because they *are* the paths - there is no
    # second, shorter spelling for a `servers` entry to make up the difference.
    assert f"{BASE}/groups/{{group}}/functions" in schema["paths"]
    assert "servers" not in schema


@pytest.mark.parametrize("page", ["/docs", "/redoc"])
def test_the_docs_publish_urls_that_answer(served_under, page):
    """Asserting the HTML alone would pass while every asset 404'd."""
    client = _client()

    html = client.get(f"{BASE_PATH}{page}").text
    assert f"{BASE_PATH}/openapi.json" in html
    assert f"{BASE_PATH}/static/" in html

    assert client.get(f"{BASE_PATH}/static/swagger-ui.css").status_code == 200
    # Nothing at the root, where another API on the same host would answer.
    assert client.get(page).status_code == 404
    assert client.get("/static/swagger-ui.css").status_code == 404


# --- the stream tickets -----------------------------------------------------


def test_a_ticket_is_minted_and_spent_on_the_same_path(served_under):
    """Mint and verify hold one string, so there is nothing to keep in step."""
    client = _client()

    minted = client.post(f"{BASE}/stream-tickets", json={"path": STREAM}).json()

    assert minted["path"] == STREAM
    assert client.get(f"{STREAM}?ticket={minted['ticket']}").status_code == 200


def test_a_path_outside_the_base_path_is_not_mintable(served_under):
    """It is not a path this API serves, so a ticket for it would open nothing."""
    client = _client()

    response = client.post(
        f"{BASE}/stream-tickets", json={"path": "/v1/groups/team/functions/foo/pods"}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_a_ticket_is_still_bound_to_one_stream(served_under):
    client = _client()

    minted = client.post(f"{BASE}/stream-tickets", json={"path": STREAM}).json()
    other = f"{BASE}/groups/team/functions/foo/stats/stream"

    assert client.get(f"{other}?ticket={minted['ticket']}").status_code == 401


# --- a different base path ------------------------------------------------------


@pytest.fixture
def under_api(monkeypatch):
    """The previous surface, which a deployment can still choose to serve."""
    get_settings.cache_clear()
    monkeypatch.setenv("SERVERLESS_BASE_PATH", "/api")
    yield
    get_settings.cache_clear()


def test_the_base_path_is_the_whole_of_the_address(under_api):
    """Nothing is pinned to the shipped default; the setting moves everything."""
    client = _client()

    assert client.get("/api/v1/functions/info").status_code == 200
    assert client.get("/api/docs").status_code == 200
    assert client.get("/api/openapi.json").status_code == 200
    # ...and the slug the chart ships is not special.
    assert client.get(f"{BASE}/functions/info").status_code == 404


async def test_the_status_url_follows_the_base_path(under_api):
    from fastapi import BackgroundTasks

    from api.models.container import ContainerCreate
    from api.services.container import ContainerService

    engine = _workload_service({"region-a": _FakeCluster("region-a")})
    spec = ContainerCreate(
        name="app", image="reg/x:1", port=8080, registryUsername="u", registryToken="t"
    )

    body = await ContainerService(engine).accept("team", spec, CALLER, BackgroundTasks())

    assert body.statusUrl == "/api/v1/groups/team/containers/app"


def test_a_browser_stream_follows_the_base_path(under_api):
    client = _client()
    stream = "/api/v1/groups/team/functions/foo/pods"

    minted = client.post("/api/v1/stream-tickets", json={"path": stream}).json()

    assert minted["path"] == stream
    assert client.get(f"{stream}?ticket={minted['ticket']}").status_code == 200
