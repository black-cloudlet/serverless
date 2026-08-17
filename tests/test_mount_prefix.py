"""The API served under a mount prefix.

Every other test reaches the app at the root, where the internal and external
paths are the same string and nothing that confuses the two can fail. Under a
prefix that confusion is a 404 on a ``statusUrl``, a blank Swagger page, or a
401 on every browser stream.

Both edge configurations are exercised - stripped and unstripped - since
Starlette routes either and a ticket must verify under both.
"""

from __future__ import annotations

import pytest
from cloudlet_apis.auth import Principal, StreamTickets
from fastapi.testclient import TestClient

from api.auth.deps import get_tickets, optional_auth, require_auth
from api.core.config import get_settings
from api.core.paths import to_external, to_internal
from api.dependencies import get_container_service, get_function_service
from api.main import create_app

# The stubs the other suites already build; this file varies the prefix, not them.
from tests.test_auth_and_deployer import _FakeCluster, _workload_service
from tests.test_stream_endpoints import FakeStreams

PREFIX = "/api/serverless"
KEY = "mount-prefix-test-signing-key-0123"  # noqa: S105 - a fixture, not a credential
CALLER = Principal(subject="u", username="alice", groups=["team"], is_admin=False)

# One stream, in both the shapes it is written in.
STREAM = "/v1/groups/team/functions/foo/pods"
EXTERNAL_STREAM = f"{PREFIX}{STREAM}"


@pytest.fixture
def prefixed(monkeypatch):
    """Settings with the API mounted under PREFIX, cleared again afterwards."""
    get_settings.cache_clear()
    monkeypatch.setenv("SERVERLESS_EXTERNAL_BASE_PATH", PREFIX)
    yield
    get_settings.cache_clear()


# --- the setting ------------------------------------------------------------


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("/api/serverless", "/api/serverless"),
        ("/api/serverless/", "/api/serverless"),  # trailing slash is not a difference
        ("/", ""),  # a root mount is what empty already means
        ("", ""),
    ],
)
def test_the_prefix_is_normalized_to_one_shape(monkeypatch, configured, expected):
    """Both directions concatenate, so two spellings would give two URLs."""
    get_settings.cache_clear()
    monkeypatch.setenv("SERVERLESS_EXTERNAL_BASE_PATH", configured)
    try:
        assert get_settings().external_base_path == expected
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


# --- the translation --------------------------------------------------------


def test_the_two_directions_round_trip(prefixed):
    assert to_external(STREAM) == EXTERNAL_STREAM
    assert to_internal(EXTERNAL_STREAM) == STREAM
    # Already internal: taking the prefix off again would corrupt it.
    assert to_internal(STREAM) == STREAM


def test_a_prefix_is_a_path_not_a_string_prefix(prefixed):
    """'/api/serverlessish' starts with '/api/serverless' and is not under it."""
    assert to_internal("/api/serverlessish/v1/x") == "/api/serverlessish/v1/x"


def test_without_a_prefix_both_directions_are_the_identity():
    get_settings.cache_clear()
    assert to_external(STREAM) == STREAM
    assert to_internal(STREAM) == STREAM


# --- what the app hands a client -------------------------------------------


async def test_status_url_carries_the_prefix(prefixed):
    """A 202's poll target is called by the client, so it is the external path.

    Through the real service: a stub returning its own ``statusUrl`` would pass
    whatever this asserted.
    """
    from fastapi import BackgroundTasks

    from api.models.container import ContainerCreate
    from api.services.container import ContainerService

    engine = _workload_service({"region-a": _FakeCluster("region-a")})
    svc = ContainerService(engine)
    spec = ContainerCreate(
        name="app", image="reg/x:1", port=8080, registryUsername="u", registryToken="t"
    )

    body = await svc.accept("team", spec, CALLER, BackgroundTasks())

    assert body.statusUrl == f"{PREFIX}/v1/groups/team/containers/app"


def test_openapi_advertises_the_prefix_as_its_server(prefixed):
    """Swagger's "Try it out" builds its URLs from this."""
    schema = TestClient(create_app()).get("/openapi.json").json()

    assert schema["servers"] == [{"url": PREFIX}]
    # Paths stay internal; carrying the prefix in both would double it.
    assert "/v1/groups/{group}/functions" in schema["paths"]


@pytest.mark.parametrize("page", ["/docs", "/redoc"])
def test_the_offline_docs_reference_the_prefix(prefixed, page):
    """Root-relative asset URLs would be fetched from the portal, not from us."""
    html = TestClient(create_app()).get(page).text

    assert f"{PREFIX}/openapi.json" in html
    assert f"{PREFIX}/static/" in html
    # A bare root-relative reference would resolve against the portal's origin.
    assert "url: '/openapi.json'" not in html
    assert '"/static/' not in html


# --- the stream tickets -----------------------------------------------------


def _streaming_client():
    """A client with the header auth stubbed and the ticket half left real."""
    app = create_app()
    svc = FakeStreams(events=[])
    app.dependency_overrides[require_auth] = lambda: CALLER
    app.dependency_overrides[optional_auth] = lambda: CALLER
    app.dependency_overrides[get_function_service] = lambda: svc
    app.dependency_overrides[get_container_service] = lambda: svc
    app.dependency_overrides[get_tickets] = lambda: StreamTickets(KEY)
    return TestClient(app)


def test_a_ticket_minted_externally_opens_the_stripped_stream(prefixed):
    """The one that 401s if mint and verify disagree about which path they hold."""
    client = _streaming_client()

    minted = client.post("/v1/stream-tickets", json={"path": EXTERNAL_STREAM}).json()
    # Echoed back as the external path, since that is the URL to open.
    assert minted["path"] == EXTERNAL_STREAM

    # ...and the stream, arriving stripped, accepts it.
    assert client.get(f"{STREAM}?ticket={minted['ticket']}").status_code == 200


def test_the_unstripped_path_reaches_the_same_stream(prefixed):
    """An edge that forwards without stripping must not break the ticket."""
    client = _streaming_client()

    minted = client.post("/v1/stream-tickets", json={"path": EXTERNAL_STREAM}).json()

    assert client.get(f"{EXTERNAL_STREAM}?ticket={minted['ticket']}").status_code == 200


def test_minting_still_accepts_the_internal_path(prefixed):
    """A CLI that knows the API's own paths is not required to know the mount."""
    client = _streaming_client()

    minted = client.post("/v1/stream-tickets", json={"path": STREAM}).json()

    assert minted["path"] == EXTERNAL_STREAM  # normalized to one answer
    assert client.get(f"{STREAM}?ticket={minted['ticket']}").status_code == 200


def test_a_ticket_is_still_bound_to_one_stream(prefixed):
    """Normalizing must not have widened what a ticket opens."""
    client = _streaming_client()

    minted = client.post("/v1/stream-tickets", json={"path": EXTERNAL_STREAM}).json()
    other = "/v1/groups/team/functions/foo/stats/stream"

    assert client.get(f"{other}?ticket={minted['ticket']}").status_code == 401


# --- the compatibility setting the chart ships ------------------------------


@pytest.fixture
def as_before(monkeypatch):
    """The chart's default: the /api/v1/... surface this API served before."""
    get_settings.cache_clear()
    monkeypatch.setenv("SERVERLESS_EXTERNAL_BASE_PATH", "/api")
    yield
    get_settings.cache_clear()


def test_the_old_paths_still_reach_the_api(as_before):
    """``externalBasePath: /api`` is what makes the base-path move invisible.

    Every existing client calls ``/api/v1/...``; the Route forwards that whole
    and Starlette matches it with ``root_path`` removed. If this breaks, the
    chart's default is no longer a safe upgrade.
    """
    client = _streaming_client()

    assert client.get("/api/v1/functions/info").status_code == 200
    assert client.get("/v1/functions/info").status_code == 200


async def test_the_old_status_url_is_what_a_client_gets_back(as_before):
    """A poll target a pre-move client can follow without knowing anything."""
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
    """The ticket path a pre-move portal mints is still the one it opens."""
    client = _streaming_client()
    old_stream = f"/api{STREAM}"

    minted = client.post("/v1/stream-tickets", json={"path": old_stream}).json()

    assert minted["path"] == old_stream
    assert client.get(f"{old_stream}?ticket={minted['ticket']}").status_code == 200
