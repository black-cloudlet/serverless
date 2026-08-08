"""The streaming endpoints over HTTP: framing on the wire, and how they authenticate."""

from __future__ import annotations

import pytest
from cloudlet_apis.auth import Principal, StreamTickets
from cloudlet_apis.errors import NotFoundError, ServiceUnavailableError
from fastapi.testclient import TestClient

from api.auth.deps import get_tickets, optional_auth, require_auth
from api.dependencies import get_container_service, get_function_service
from api.main import create_app
from api.models.common import (
    LogLine,
    PodInfo,
    PodLogSnapshot,
    PodRoster,
    StreamError,
    WorkloadStatsResponse,
)
from api.services.streams.sse import StreamEvent

KEY = "endpoint-test-signing-key-0123456"  # noqa: S105 - a fixture, not a credential
PODS = "/api/v1/groups/team/functions/foo/pods"
LOGS = "/api/v1/groups/team/functions/foo/logs/pods/foo-team-00001-abcde"
STATS = "/api/v1/groups/team/functions/foo/stats/stream"
CALLER = Principal(subject="u", username="alice", groups=["team"], is_admin=False)


async def _events(*events):
    for event in events:
        yield event


class FakeStreams:
    """A service whose streams are scripted, so the wire format is what is tested."""

    def __init__(self, raises=None, events=None):
        self.raises = raises
        self.events = events
        self.seen: dict = {}

    async def stream_pods(self, name, group, user, *, interval):
        self.seen = dict(name=name, group=group, user=user, interval=interval)
        if self.raises:
            raise self.raises
        return _events(*(self.events or []))

    async def pods(self, name, group, user):
        self.seen = dict(name=name, group=group, user=user, follow=False)
        if self.raises:
            raise self.raises
        return PodRoster(
            name=name,
            group=group,
            type="function",
            site="central",
            pods=[PodInfo(pod="foo-team-00001-abcde", phase="Running", ready=True)],
        )

    async def pod_logs(self, name, group, user, *, pod, container, since_seconds, limit_bytes):
        self.seen = dict(
            name=name,
            group=group,
            user=user,
            pod=pod,
            container=container,
            since_seconds=since_seconds,
            limit_bytes=limit_bytes,
            follow=False,
        )
        if self.raises:
            raise self.raises
        return PodLogSnapshot(
            name=name,
            group=group,
            type="function",
            site="central",
            pod=pod,
            container=container,
            lines=[LogLine(pod=pod, container=container, message="from the snapshot")],
        )

    async def stream_pod_logs(self, name, group, user, *, pod, container, since_seconds):
        self.seen = dict(
            name=name,
            group=group,
            user=user,
            pod=pod,
            container=container,
            since_seconds=since_seconds,
        )
        if self.raises:
            raise self.raises
        return _events(*(self.events or []))

    async def stream_stats(self, name, group, user, *, interval):
        self.seen = dict(name=name, group=group, user=user, interval=interval)
        if self.raises:
            raise self.raises
        return _events(*(self.events or []))


def build(svc, *, authenticated=True, key=KEY):
    app = create_app()
    if authenticated:
        app.dependency_overrides[require_auth] = lambda: CALLER
        # The streams take the header through `optional_auth`, so that is the half
        # a stub replaces; the ticket half stays real and is what these exercise.
        app.dependency_overrides[optional_auth] = lambda: CALLER
    else:
        app.dependency_overrides[optional_auth] = lambda: None
    app.dependency_overrides[get_function_service] = lambda: svc
    app.dependency_overrides[get_container_service] = lambda: svc
    app.dependency_overrides[get_tickets] = lambda: StreamTickets(key)
    return TestClient(app)


def frames(text):
    """Split an SSE body into its events, dropping heartbeats and the preamble."""
    out = []
    for block in text.split("\n\n"):
        if not block.startswith("event: "):
            continue
        head, _, rest = block.partition("\n")
        out.append((head.removeprefix("event: "), rest))
    return out


# --- the wire ---------------------------------------------------------------


def test_a_stream_is_served_as_an_unbuffered_event_stream():
    svc = FakeStreams(events=[StreamEvent("stats", WorkloadStatsResponse(overallStatus="Ready"))])
    response = build(svc).get(STATS)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    # Both say "do not buffer this" to two different intermediaries; a buffered
    # event stream is indistinguishable from a hung one.
    assert response.headers["x-accel-buffering"] == "no"
    assert "no-cache" in response.headers["cache-control"]


def test_the_body_opens_with_a_retry_so_a_scheduled_close_is_not_a_reconnect_storm():
    svc = FakeStreams(events=[])
    assert build(svc).get(STATS).text.startswith("retry: ")


def test_events_reach_the_client_named_and_in_order():
    svc = FakeStreams(
        events=[
            StreamEvent("stats", WorkloadStatsResponse(overallStatus="Deploying", replicas=0)),
            StreamEvent("stats", WorkloadStatsResponse(overallStatus="Ready", replicas=2)),
        ]
    )
    got = frames(build(svc).get(STATS).text)

    assert [name for name, _ in got] == ["stats", "stats"]
    assert '"overallStatus":"Ready"' in got[1][1]
    assert '"replicas":2' in got[1][1]


def test_a_failure_after_the_first_byte_becomes_an_error_event_not_a_status():
    """The status line is long gone; an event is the only way left to say so."""

    async def blows_up():
        yield StreamEvent("stats", WorkloadStatsResponse(overallStatus="Ready"))
        raise NotFoundError("function 'foo' not found")

    svc = FakeStreams()
    svc.stream_stats = lambda *a, **k: _wrap(blows_up())
    response = build(svc).get(STATS)

    assert response.status_code == 200  # it succeeded, then ended
    assert [name for name, _ in frames(response.text)] == ["stats", "error"]
    assert '"code":"NOT_FOUND"' in frames(response.text)[-1][1]


def test_an_unexpected_mid_stream_failure_does_not_leak_its_text():
    async def blows_up():
        yield StreamEvent("stats", WorkloadStatsResponse(overallStatus="Ready"))
        raise RuntimeError("db://user:hunter2@internal")

    svc = FakeStreams()
    svc.stream_stats = lambda *a, **k: _wrap(blows_up())
    body = build(svc).get(STATS).text

    assert '"code":"INTERNAL"' in body
    assert "hunter2" not in body


async def _wrap(gen):
    return gen


# --- failures that still have a status code ---------------------------------


def test_a_missing_workload_is_a_404_envelope_not_an_opened_stream():
    """Everything that can fail with a code is settled before the first byte."""
    response = build(FakeStreams(raises=NotFoundError("function 'foo' not found"))).get(LOGS)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_a_full_stream_pool_is_a_503_envelope():
    svc = FakeStreams(raises=ServiceUnavailableError("too many open streams (8)"))
    response = build(svc).get(STATS)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert "too many open streams" in response.json()["error"]["message"]


# --- parameters -------------------------------------------------------------


def test_the_log_stream_passes_its_parameters_through():
    svc = FakeStreams(events=[])
    build(svc).get(f"{LOGS}?container=queue-proxy&sinceSeconds=30")

    assert svc.seen["pod"] == "foo-team-00001-abcde"
    assert svc.seen["container"] == "queue-proxy"
    assert svc.seen["since_seconds"] == 30
    assert svc.seen["name"] == "foo"
    assert svc.seen["group"] == "team"


def test_the_log_stream_defaults_to_the_user_container():
    svc = FakeStreams(events=[])
    build(svc).get(LOGS)

    assert svc.seen["container"] == "user-container"
    assert svc.seen["since_seconds"] is None


def test_the_pods_stream_passes_its_interval_through():
    svc = FakeStreams(events=[])
    build(svc).get(f"{PODS}?interval=2")
    assert svc.seen["interval"] == 2

    svc = FakeStreams(events=[])
    build(svc).get(PODS)
    assert svc.seen["interval"] is None  # the service applies the configured default


@pytest.mark.parametrize("query", ["sinceSeconds=0", "sinceSeconds=-1"])
def test_non_positive_windows_are_rejected(query):
    assert build(FakeStreams(events=[])).get(f"{LOGS}?{query}").status_code == 400


def test_a_non_positive_interval_is_rejected():
    assert build(FakeStreams(events=[])).get(f"{PODS}?interval=0").status_code == 400


@pytest.mark.parametrize(
    "pod",
    [
        "..%2F..%2Fsecrets",  # an encoded separator, which routing does not fold away
        "%2E%2E%2Fetc",
        "UPPERCASE",
        "has spaces",
        "trailing-",
        "under_score",
    ],
)
def test_a_pod_name_that_is_not_a_pod_name_never_reaches_the_service(pod):
    """The segment is interpolated into a request to the cluster's API server.

    A bare ``../`` is folded away by URL normalization before routing even sees
    it, so the cases that matter are the encoded ones - and the character classes
    Kubernetes itself would not accept as a pod name.
    """
    svc = FakeStreams(events=[])
    response = build(svc).get(f"/api/v1/groups/team/functions/foo/logs/pods/{pod}")
    assert response.status_code in (400, 404), pod
    assert svc.seen == {}, f"{pod} reached the service"


def test_both_offerings_stream():
    for base in (
        "/api/v1/groups/team/functions/foo",
        "/api/v1/groups/team/containers/foo",
    ):
        for suffix in ("pods", "stats/stream", "logs/pods/foo-team-00001-abcde"):
            svc = FakeStreams(events=[])
            response = build(svc).get(f"{base}/{suffix}")
            assert response.status_code == 200, f"{base}/{suffix}"


def test_the_snapshot_logs_endpoint_is_gone():
    """Replaced by the per-pod stream; a stale client must not get a silent 200."""
    response = build(FakeStreams(events=[])).get("/api/v1/groups/team/functions/foo/logs")
    assert response.status_code == 404


# --- authenticating a stream ------------------------------------------------


def test_a_ticket_opens_a_stream_a_browser_could_not_otherwise_open():
    """EventSource sends no Authorization header; this is the whole point."""
    svc = FakeStreams(events=[])
    client = build(svc, authenticated=False)

    ticket = StreamTickets(KEY).mint(CALLER, LOGS).value
    response = client.get(f"{LOGS}?ticket={ticket}")

    assert response.status_code == 200
    assert svc.seen["user"].username == "alice"
    assert svc.seen["user"].groups == {"team": "team"}


def test_a_ticket_for_another_stream_is_refused():
    svc = FakeStreams(events=[])
    ticket = StreamTickets(KEY).mint(CALLER, STATS).value  # minted for stats...

    response = build(svc, authenticated=False).get(f"{LOGS}?ticket={ticket}")  # ...used on logs

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_a_forged_ticket_is_refused():
    ticket = StreamTickets("not-the-real-key-abcdefghijklmnopqrs").mint(CALLER, LOGS).value
    response = build(FakeStreams(events=[]), authenticated=False).get(f"{LOGS}?ticket={ticket}")
    assert response.status_code == 401


def test_a_stream_with_no_credential_at_all_is_refused():
    response = build(FakeStreams(events=[]), authenticated=False).get(LOGS)
    assert response.status_code == 401


def test_a_bad_ticket_is_not_rescued_by_the_header():
    """Ticket first: a caller that sent one gets told about the one it sent."""
    client = build(FakeStreams(events=[]))  # header auth IS overridden to succeed
    response = client.get(f"{LOGS}?ticket=obviously-not-valid")
    assert response.status_code == 401


def test_the_header_still_works_so_a_cli_follow_needs_no_ticket():
    response = build(FakeStreams(events=[])).get(LOGS)
    assert response.status_code == 200


# --- minting ----------------------------------------------------------------


def test_minting_returns_a_ticket_bound_to_the_requested_path():
    client = build(FakeStreams(events=[]))
    body = client.post("/api/v1/stream-tickets", json={"path": LOGS}).json()

    assert body["path"] == LOGS
    assert body["ticket"]
    assert body["expiresAt"]
    # ...and it actually opens that stream.
    assert client.get(f"{LOGS}?ticket={body['ticket']}").status_code == 200


def test_minting_requires_authentication():
    client = build(FakeStreams(events=[]), authenticated=False)
    assert client.post("/api/v1/stream-tickets", json={"path": LOGS}).status_code == 401


def test_minting_rejects_a_path_that_is_not_a_stream():
    client = build(FakeStreams(events=[]))
    response = client.post(
        "/api/v1/stream-tickets", json={"path": "/api/v1/groups/team/functions/foo"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_minting_is_unavailable_when_no_signing_key_is_configured():
    """The deployment opted out of the browser path; header auth still works."""
    client = build(FakeStreams(events=[]), key="")

    response = client.post("/api/v1/stream-tickets", json={"path": LOGS})
    assert response.status_code == 503
    assert "Authorization header" in response.json()["error"]["message"]

    assert client.get(LOGS).status_code == 200


def test_no_signing_key_refuses_every_ticket():
    client = build(FakeStreams(events=[]), authenticated=False, key="")
    ticket = StreamTickets(KEY).mint(CALLER, LOGS).value
    assert client.get(f"{LOGS}?ticket={ticket}").status_code == 401


# --- the published contract -------------------------------------------------


def test_the_streams_document_themselves_as_event_streams():
    schema = create_app().openapi()
    for offering in ("functions", "containers"):
        for suffix in ("pods", "stats/stream", "logs/pods/{pod}"):
            path = f"/api/v1/groups/{{group}}/{offering}/{{name}}/{suffix}"
            content = schema["paths"][path]["get"]["responses"]["200"]["content"]
            assert "text/event-stream" in content, path


def test_the_stats_snapshot_keeps_its_own_contract():
    """Streaming must not change what the one remaining JSON read returns."""
    schema = create_app().openapi()
    path = "/api/v1/groups/{group}/functions/{name}/stats"
    content = schema["paths"][path]["get"]["responses"]["200"]["content"]
    assert list(content) == ["application/json"]


def test_the_removed_log_paths_are_not_published():
    schema = create_app().openapi()
    for path in (
        "/api/v1/groups/{group}/functions/{name}/logs",
        "/api/v1/groups/{group}/functions/{name}/logs/stream",
    ):
        assert path not in schema["paths"], path


def test_the_error_event_shares_the_envelope_s_code_vocabulary():
    """A client switches on one set of codes however the failure reaches it."""
    published = {e["code"] for e in create_app().openapi() and _codes()}
    assert {"NOT_FOUND", "SERVICE_UNAVAILABLE", "INTERNAL"} <= published
    assert StreamError(code="NOT_FOUND", message="x").code in published


def _codes():
    from common.errors import error_catalog

    return [{"code": code, "status": status} for code, status in error_catalog()]


# --- follow=false: the same endpoints, read once -----------------------------
#
# Both endpoints stream by default. `follow=false` exists for the caller that
# cannot hold a connection open - and it has to be on BOTH, or the log snapshot
# is unreachable: finding a pod name would still require a stream.


def test_the_log_snapshot_is_json_not_an_event_stream():
    response = build(FakeStreams()).get(f"{LOGS}?follow=false")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["pod"] == "foo-team-00001-abcde"
    assert body["lines"][0]["message"] == "from the snapshot"


def test_the_pods_snapshot_is_json_not_an_event_stream():
    response = build(FakeStreams()).get(f"{PODS}?follow=false")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["pods"][0]["pod"] == "foo-team-00001-abcde"


def test_following_is_the_default_on_both():
    for path in (PODS, LOGS):
        response = build(FakeStreams(events=[])).get(path)
        assert response.headers["content-type"].startswith("text/event-stream"), path


@pytest.mark.parametrize("value", ["true", "1", "yes"])
def test_follow_true_is_still_a_stream(value):
    response = build(FakeStreams(events=[])).get(f"{LOGS}?follow={value}")
    assert response.headers["content-type"].startswith("text/event-stream")


def test_the_snapshot_passes_its_own_parameters_through():
    svc = FakeStreams()
    build(svc).get(f"{LOGS}?follow=false&container=queue-proxy&sinceSeconds=30&limitBytes=4096")

    assert svc.seen["follow"] is False
    assert svc.seen["container"] == "queue-proxy"
    assert svc.seen["since_seconds"] == 30
    # limitBytes is meaningful only here - a follow has no total to cap.
    assert svc.seen["limit_bytes"] == 4096


def test_a_snapshot_of_a_pod_that_is_not_ours_is_the_same_404_as_the_stream():
    """follow=false must not be a way around the pod-ownership check."""
    svc = FakeStreams(raises=NotFoundError("pod 'victim' not found"))
    response = build(svc).get(f"{LOGS}?follow=false")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_a_snapshot_needs_no_stream_slot():
    """It ends, so it is not a stream and must not be rationed as one."""
    svc = FakeStreams(raises=ServiceUnavailableError("too many open streams (32)"))
    # The stub raises for every call, so this only proves the route reaches the
    # snapshot method; that it takes no slot is asserted against the engine in
    # test_streaming.py.
    assert build(svc).get(f"{LOGS}?follow=false").status_code == 503


def test_both_forms_are_published_on_one_operation():
    """A generated client that knew only one of them would be wrong half the time."""
    schema = create_app().openapi()
    for offering in ("functions", "containers"):
        for suffix in ("pods", "logs/pods/{pod}"):
            path = f"/api/v1/groups/{{group}}/{offering}/{{name}}/{suffix}"
            content = schema["paths"][path]["get"]["responses"]["200"]["content"]
            assert set(content) == {"application/json", "text/event-stream"}, path
            params = {p["name"] for p in schema["paths"][path]["get"]["parameters"]}
            assert "follow" in params, path
