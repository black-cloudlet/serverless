"""API routing tests with auth and services stubbed (no cluster needed)."""

import pytest
from fastapi.testclient import TestClient

from api.auth.claims import Principal
from api.auth.deps import require_auth
from api.dependencies import get_container_service, get_function_service
from api.main import create_app
from api.models.common import SiteStatus
from api.models.container import ContainerResponse
from api.models.function import FunctionResponse


def _model(kind, **fields):
    cls = FunctionResponse if kind == "function" else ContainerResponse
    return cls(**fields)


def _accepted(kind, name, group, **extra):
    return _model(
        kind,
        name=name,
        group=group,
        type=kind,
        hostname=f"{name}.serverless.example.com",
        overallStatus="Pending",
        sites=[],
        statusUrl=f"/api/v1/groups/{group}/{kind}s/{name}",
        **extra,
    )


def _ready(kind, name, group="team", **extra):
    return _model(
        kind,
        name=name,
        group=group,
        type=kind,
        hostname="x.serverless.example.com",
        overallStatus="Ready",
        sites=[SiteStatus(site="site-a", status="Ready")],
        **extra,
    )


class FakeFunctions:
    async def accept(self, group, spec, user, background):
        return _accepted("function", spec.name, group, runtime=spec.runtime)

    async def accept_update(self, group, name, spec, user, background):
        return _accepted("function", name, group)

    async def get(self, name, group, user):
        return _ready(
            "function", name, runtime="python", gitRepo="https://git/x.git", branch="main"
        )

    async def logs(self, name, group, user, *, container, since_seconds, limit_bytes):
        from api.models.common import LogsResponse, PodLogs

        return LogsResponse(
            name=name,
            group=group,
            type="function",
            site="site-a",
            pods=[PodLogs(pod=f"{name}-{group}-00001-x", container=container, logs="hello")],
        )

    async def list(self, group, user, sort="name"):
        from api.models.common import WorkloadSummary

        return [
            WorkloadSummary(
                name="fn-a",
                group="team",
                type="function",
                hostname="fn-a.example.com",
                overallStatus="Ready",
                size="small",
                sites=["central"],
            )
        ]

    async def delete(self, name, group, user):
        return None


class FakeContainers:
    async def accept(self, group, spec, user, background):
        return _accepted("container", spec.name, group, image=spec.image)

    async def accept_update(self, group, name, spec, user, background):
        return _accepted("container", name, group, image=spec.image or "kept:1")

    async def get(self, name, group, user):
        return _ready("container", name, image="reg/x:1", registryUsername="svc-team")

    async def logs(self, name, group, user, *, container, since_seconds, limit_bytes):
        from api.models.common import LogsResponse, PodLogs

        return LogsResponse(
            name=name,
            group=group,
            type="container",
            site="site-a",
            pods=[PodLogs(pod=f"{name}-{group}-00001-x", container=container, logs="hi")],
        )

    async def list(self, group, user, sort="name"):
        from api.models.common import WorkloadSummary

        return [
            WorkloadSummary(
                name="ctr-a",
                group="team",
                type="container",
                hostname="ctr-a.example.com",
                overallStatus="Ready",
                size="medium",
                sites=["central", "south"],
            )
        ]

    async def delete(self, name, group, user):
        return None


@pytest.fixture
def client():
    app = create_app()
    app.dependency_overrides[require_auth] = lambda: Principal(
        subject="u", username="alice", groups=["team"], is_admin=False
    )
    app.dependency_overrides[get_function_service] = lambda: FakeFunctions()
    app.dependency_overrides[get_container_service] = lambda: FakeContainers()
    return TestClient(app)


def test_healthz_no_auth():
    c = TestClient(create_app())
    assert c.get("/healthz").json() == {"status": "ok"}


def test_info_is_public_and_static():
    # No auth override applied: both info paths must be reachable unauthenticated.
    c = TestClient(create_app())

    # Shared platform fields appear on both offerings' documents.
    for path in ("/api/v1/containers/info", "/api/v1/functions/info"):
        body = c.get(path).json()
        assert body["version"]
        assert isinstance(body["sites"], list)
        assert body["sizes"] == ["small", "medium", "large"]
        assert body["routeDomain"]
        assert body["defaultHostTemplate"] == "{name}-{group}.{routeDomain}"

    # container-only: the port rules
    cont = c.get("/api/v1/containers/info").json()
    assert cont["port"] == {"required": True, "min": 1, "max": 65535}
    assert "runtimes" not in cont

    # function-only: the available runtimes
    fn = c.get("/api/v1/functions/info").json()
    assert "python" in [r["name"] for r in fn["runtimes"]]
    assert "port" not in fn
    metrics = {m["name"]: m for m in body["scaling"]["metrics"]}
    assert metrics["concurrency"]["minScaleFloor"] == 0
    assert metrics["concurrency"]["target"]["default"] == 100
    assert metrics["concurrency"]["target"]["max"] is None
    assert metrics["cpu"]["minScaleFloor"] == 1
    assert metrics["cpu"]["target"]["default"] == 70
    assert metrics["cpu"]["target"]["max"] == 100
    assert body["scaling"]["defaultMetric"] == "concurrency"
    assert body["scaling"]["scaleDownDelay"]["max"] == "1h"


async def test_startup_warmup_is_best_effort(monkeypatch):
    """A failing OIDC discovery / cluster connect must not crash startup."""
    from api.core.config import Settings, SiteConfig, SSOConfig
    from api.main import _warmup
    from api.services.deployer import Deployer

    class _BoomValidator:
        def warmup(self):
            raise RuntimeError("sso down")

    monkeypatch.setattr("api.main.get_validator", lambda: _BoomValidator())

    settings = Settings(
        auth_enabled=True,
        sso=SSOConfig(),
        sites=[SiteConfig(name="site-a", cluster="site-a-0")],
        cluster_connect_timeout=0.01,
        cluster_read_timeout=0.01,
    )

    class _BoomCluster:
        site = "site-a"

        def connect(self):
            raise RuntimeError("cluster unreachable")

    deployer = Deployer(settings)
    deployer._clusters = {"site-a": _BoomCluster()}
    # Should complete without raising even though both warmups fail.
    await _warmup(settings, deployer)


def test_cors_allows_configured_origin(monkeypatch):
    from api.core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("SERVERLESS_CORS_ALLOW_ORIGINS", '["https://acme.service-now.com"]')
    try:
        c = TestClient(create_app())
        r = c.get("/healthz", headers={"Origin": "https://acme.service-now.com"})
        assert r.headers.get("access-control-allow-origin") == "https://acme.service-now.com"
    finally:
        get_settings.cache_clear()


def test_create_container_accepted(client):
    r = client.post(
        "/api/v1/groups/team/containers",
        json={
            "name": "orders-api",
            "image": "registry.internal/team/orders:1",
            "port": 8080,
            "registryUsername": "u",
            "registryToken": "t",
        },
    )
    assert r.status_code == 202
    body = r.json()
    assert body["type"] == "container"
    assert body["overallStatus"] == "Pending"
    assert body["statusUrl"] == "/api/v1/groups/team/containers/orders-api"


def test_create_container_validation_error(client):
    r = client.post("/api/v1/groups/team/containers", json={"name": "BAD NAME"})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert err["status"] == 400  # the numeric status is in the envelope too


def test_info_publishes_each_runtime_with_the_versions_a_build_accepts():
    """A version picker cannot be built from names alone.

    The versions come from the same ConfigMap the builder reads, so what is
    advertised is what a build will accept.
    """
    c = TestClient(create_app())
    runtimes = {r["name"]: r for r in c.get("/api/v1/functions/info").json()["runtimes"]}

    assert runtimes["python"]["versions"] == ["3.11", "3.12"]
    assert runtimes["python"]["defaultVersion"] == "3.12"
    assert runtimes["node"]["versions"] == ["18", "20", "22"]


def test_info_publishes_the_status_and_error_vocabularies():
    """Everything a client would otherwise hardcode and let drift."""
    from typing import get_args

    from api.models.common import SITE_STATUSES, WorkloadStatus
    from common.errors import ValidationError, error_catalog

    body = TestClient(create_app()).get("/api/v1/containers/info").json()

    # derived from the Literal the responses are typed with, not a second list
    assert body["statuses"]["workload"] == list(get_args(WorkloadStatus))
    assert body["statuses"]["site"] == list(SITE_STATUSES)
    # a poller needs to know which values mean "stop"
    assert set(body["statuses"]["terminal"]) < set(body["statuses"]["workload"])
    assert "Building" not in body["statuses"]["terminal"]

    codes = {e["code"]: e["status"] for e in body["errorCodes"]}
    assert codes[ValidationError.code] == ValidationError.status_code
    assert len(codes) == len(error_catalog())


def test_info_publishes_the_combined_name_and_group_limit():
    """No per-field schema can carry this, so /info has to.

    Each half may be 63 characters on its own; it is the join that becomes the
    KSVC name. A form validating the fields separately would accept a pair the
    API rejects, so the rule is published for the client to apply.
    """
    from common.names import MAX_OBJECT_NAME, object_name

    naming = TestClient(create_app()).get("/api/v1/containers/info").json()["naming"]

    # composed by the same function the platform names objects with
    assert naming["template"] == object_name("{name}", "{group}")
    assert naming["maxLength"] == MAX_OBJECT_NAME
    # the pair the rule exists to catch: both halves legal, the join is not
    assert len("n" * 40) <= 63 and len("g" * 40) <= 63
    assert len(object_name("n" * 40, "g" * 40)) > naming["maxLength"]


def test_the_error_catalog_is_walked_off_the_exception_classes():
    """A hand-kept list is what goes stale, so a new error must publish itself."""
    import gc

    from common.errors import APIError, error_catalog

    class TeapotError(APIError):
        status_code = 418
        code = "TEAPOT"

    try:
        assert ("TEAPOT", 418) in error_catalog()
    finally:
        # __subclasses__ holds weak references, so dropping the class and
        # collecting keeps this test out of every later catalog.
        del TeapotError
        gc.collect()

    assert "TEAPOT" not in dict(error_catalog())


def test_framework_http_errors_get_a_meaningful_code_and_status(client):
    # Unknown route / wrong method are framework HTTP errors (not domain errors);
    # the code is derived from the status, not a flat "HTTP_ERROR".
    r = client.get("/api/v1/does-not-exist")
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["status"] == 404 and err["code"] == "NOT_FOUND"

    # POST to a path that only serves GET/PUT/DELETE -> 405
    r = client.post("/api/v1/groups/team/functions/foo")
    assert r.status_code == 405
    err = r.json()["error"]
    assert err["status"] == 405 and err["code"] == "METHOD_NOT_ALLOWED"


def test_get_function(client):
    r = client.get("/api/v1/groups/team/functions/foo")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "foo" and body["group"] == "team"
    # FunctionResponse is flat and mirrors the create body: function source fields
    # are present, container-only fields are absent.
    assert body["runtime"] == "python" and body["gitRepo"] == "https://git/x.git"
    # the built image is internal to functions; not exposed
    assert "registryUsername" not in body
    assert "image" not in body and "imageDigest" not in body


def test_get_container_shape(client):
    r = client.get("/api/v1/groups/team/containers/foo")
    assert r.status_code == 200
    body = r.json()
    # ContainerResponse mirrors the create body: image + registryUsername present,
    # function-only fields (gitRepo/runtime) absent.
    assert body["image"] == "reg/x:1" and body["registryUsername"] == "svc-team"
    assert "gitRepo" not in body and "runtime" not in body


def test_get_function_logs(client):
    r = client.get("/api/v1/groups/team/functions/foo/logs")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "foo" and body["type"] == "function" and body["site"] == "site-a"
    assert body["pods"][0]["container"] == "user-container"  # default
    assert body["pods"][0]["logs"] == "hello"


def test_get_container_logs_with_params(client):
    r = client.get(
        "/api/v1/groups/team/containers/foo/logs?container=queue-proxy"
        "&sinceSeconds=300&limitBytes=4096"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "container"
    assert body["pods"][0]["container"] == "queue-proxy"  # override honored


def test_logs_rejects_non_positive_window(client):
    # sinceSeconds must be > 0; the RequestValidationError maps to 400
    assert client.get("/api/v1/groups/team/functions/foo/logs?sinceSeconds=0").status_code == 400
    assert client.get("/api/v1/groups/team/containers/foo/logs?limitBytes=0").status_code == 400


def test_path_name_validated_at_the_edge(client):
    """A path {name} that isn't a DNS-1123 label is rejected at the boundary (400),
    like the request-body name - not passed through to a cluster lookup."""
    assert client.get("/api/v1/groups/team/functions/Bad_Name").status_code == 400
    assert client.delete("/api/v1/groups/team/containers/UPPER").status_code == 400


def test_list_functions(client):
    r = client.get("/api/v1/groups/team/functions")
    assert r.status_code == 200
    body = r.json()
    assert [w["name"] for w in body] == ["fn-a"]
    assert body[0]["type"] == "function" and body[0]["group"] == "team"
    assert body[0]["sites"] == ["central"]


def test_list_containers(client):
    r = client.get("/api/v1/groups/team/containers")
    assert r.status_code == 200
    body = r.json()
    assert body[0]["name"] == "ctr-a"
    assert body[0]["size"] == "medium" and body[0]["sites"] == ["central", "south"]


def test_list_accepts_sort_and_rejects_unknown(client):
    assert client.get("/api/v1/groups/team/functions?sort=createdAt").status_code == 200
    assert client.get("/api/v1/groups/team/functions?sort=name").status_code == 200
    assert client.get("/api/v1/groups/team/functions?sort=bogus").status_code == 400


def test_path_group_is_normalized_at_the_edge():
    """A ggd-/slash-prefixed group in the path is normalized before it reaches the
    service - the same one-place-at-the-edge normalization the request body used to
    get, so nothing downstream re-normalizes."""
    seen = {}

    class _Capture(FakeFunctions):
        async def get(self, name, group, user):
            seen["group"] = group
            return _ready("function", name, runtime="python")

    app = create_app()
    app.dependency_overrides[require_auth] = lambda: Principal(
        subject="u", username="alice", groups=["platforms"], is_admin=False
    )
    app.dependency_overrides[get_function_service] = lambda: _Capture()
    c = TestClient(app)

    r = c.get("/api/v1/groups/ggd-1234-platforms/functions/foo")
    assert r.status_code == 200
    assert seen["group"] == "platforms"  # normalized at the router boundary


def test_path_group_accepts_underscores_and_case_and_folds_them():
    """Any spelling of an SSO group works in the path - underscores and mixed case
    alike reach the service as the canonical lowercase hyphenated form, so they all
    address one resource."""
    seen = []

    class _Capture(FakeFunctions):
        async def get(self, name, group, user):
            seen.append(group)
            return _ready("function", name, runtime="python")

    app = create_app()
    app.dependency_overrides[require_auth] = lambda: Principal(
        subject="u", username="alice", groups=["my-team"], is_admin=False
    )
    app.dependency_overrides[get_function_service] = lambda: _Capture()
    c = TestClient(app)

    for path_group in ("my_team", "my-team", "My_Team", "MY-TEAM"):
        r = c.get(f"/api/v1/groups/{path_group}/functions/foo")
        assert r.status_code == 200
    assert seen == ["my-team"] * 4


def test_update_container_accepted(client):
    r = client.put(
        "/api/v1/groups/team/containers/orders-api",
        json={
            "image": "registry.internal/team/orders:2",
            "port": 8080,
            "scaling": {"minScale": 1},
        },
    )
    assert r.status_code == 202
    assert r.json()["image"] == "registry.internal/team/orders:2"
    assert r.json()["overallStatus"] == "Pending"


def test_update_function_accepted(client):
    r = client.put(
        "/api/v1/groups/team/functions/foo",
        json={
            "gitRepo": "https://git/x.git",
            "runtime": "python",
            "env": [{"name": "X", "value": "1"}],
        },
    )
    assert r.status_code == 202
    assert r.json()["type"] == "function"


def test_update_container_username_only_kept(client):
    # Echoing the redacted read (username shown, no token) keeps the existing
    # credential - accepted, not a 400.
    r = client.put(
        "/api/v1/groups/team/containers/orders-api",
        json={
            "image": "registry.internal/team/orders:2",
            "port": 8080,
            "registryUsername": "bob",  # token omitted -> keep
        },
    )
    assert r.status_code == 202


def test_update_container_token_without_username_rejected(client):
    r = client.put(
        "/api/v1/groups/team/containers/orders-api",
        json={
            "image": "registry.internal/team/orders:2",
            "port": 8080,
            "registryToken": "t",  # username missing -> meaningless
        },
    )
    assert r.status_code == 400


def test_update_function_build_change_accepted_without_token(client):
    # The token is stored, so changing a build input does not need it re-sent:
    # the request is accepted and the rebuild uses the stored one.
    r = client.put(
        "/api/v1/groups/team/functions/foo",
        json={
            "gitRepo": "https://git/x.git",
            "runtime": "python",
            "branch": "release",  # gitToken omitted -> reuse stored token
        },
    )
    assert r.status_code == 202


def test_docs_served_offline_from_vendored_assets():
    """Swagger UI / ReDoc must load local assets, not the jsdelivr CDN (airgap)."""
    from api.main import create_app

    client = TestClient(create_app())

    # OpenAPI schema is served by the app itself.
    assert client.get("/openapi.json").status_code == 200

    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "/static/swagger-ui-bundle.js" in docs.text
    assert "/static/swagger-ui.css" in docs.text
    assert "cdn.jsdelivr.net" not in docs.text  # no CDN dependency

    redoc = client.get("/redoc")
    assert redoc.status_code == 200
    assert "/static/redoc.standalone.js" in redoc.text
    assert "cdn.jsdelivr.net" not in redoc.text
    assert "fonts.googleapis.com" not in redoc.text  # google fonts disabled

    # The vendored static files are actually served.
    css = client.get("/static/swagger-ui.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert client.get("/static/swagger-ui-bundle.js").status_code == 200
    assert client.get("/static/redoc.standalone.js").status_code == 200


def test_swagger_sso_login_wired_in_openapi():
    """Swagger UI's Authorize uses an OAuth2 PKCE flow with the public client id;
    no secret, and require_auth still enforces (this is docs-only)."""
    from api.main import create_app

    app = create_app()  # auth_enabled defaults True
    schema = app.openapi()
    sso = schema["components"]["securitySchemes"]["SSO"]
    assert sso["type"] == "oauth2"
    flow = sso["flows"]["authorizationCode"]
    assert flow["authorizationUrl"].endswith("/protocol/openid-connect/auth")
    assert flow["tokenUrl"].endswith("/protocol/openid-connect/token")

    init = app.swagger_ui_init_oauth
    assert init["clientId"] == "serverless-api-swagger"
    assert init["usePkceWithAuthorizationCodeGrant"] is True
    assert "clientSecret" not in init  # public client - no secret


def test_swagger_docs_html_delivers_oauth_init():
    """The vendored /docs HTML must call initOAuth with the client id + PKCE.

    Configuring app.swagger_ui_init_oauth is not enough - the offline docs route
    has to forward it to get_swagger_ui_html, or Swagger's Authorize modal falls
    back to asking for a client id and secret. Assert the delivery path, not just
    the config object.
    """
    from api.main import create_app

    docs = TestClient(create_app()).get("/docs")
    assert docs.status_code == 200
    assert "initOAuth" in docs.text
    assert "serverless-api-swagger" in docs.text  # client id pre-filled
    assert "usePkceWithAuthorizationCodeGrant" in docs.text  # PKCE, no secret


def _client_raising(exc: Exception) -> TestClient:
    """A client whose container service raises ``exc``, with 500s returned not re-raised."""
    from api.dependencies import get_container_service

    class _Boom:
        async def get(self, name, group, user):
            raise exc

    app = create_app()
    app.dependency_overrides[require_auth] = lambda: Principal(
        subject="u", username="alice", groups=["team"], is_admin=False
    )
    app.dependency_overrides[get_container_service] = lambda: _Boom()
    return TestClient(app, raise_server_exceptions=False)


def test_an_unanticipated_error_still_returns_the_documented_envelope():
    """A 500 is the response a caller most needs to be able to report.

    Served by Starlette's default it is plain text with no `error` object, so a
    client parsing the envelope /info advertises breaks inside its own error
    path, and there is no id to tie the report to the traceback.
    """
    client = _client_raising(RuntimeError("connection to db-master.internal failed"))
    r = client.get("/api/v1/groups/team/containers/app", headers={"X-Request-ID": "trace-me-123"})

    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/json")
    err = r.json()["error"]
    assert err["status"] == 500
    assert err["code"] == "INTERNAL"
    # the correlation id survives in the body AND the header - the 500 used to be
    # the one response carrying it nowhere, because ServerErrorMiddleware sits
    # outside the middleware that stamps it
    assert err["requestId"] == "trace-me-123"
    assert r.headers["x-request-id"] == "trace-me-123"


def test_an_unanticipated_error_does_not_leak_the_exception_text():
    """Exception text routinely carries internal hostnames or secret material."""
    client = _client_raising(RuntimeError("connection to db-master.internal failed"))
    body = client.get("/api/v1/groups/team/containers/app").text

    assert "db-master.internal" not in body
    assert "RuntimeError" not in body
    assert body.count("Internal server error.") == 1


def test_the_unhandled_error_is_logged_with_the_id_the_client_was_given():
    """The detail belongs in the log - which is only useful if it carries the id.

    The handler runs after the request has unwound and the context var the log
    filter normally reads is back to "-", so the id has to be stamped explicitly.

    Captured on the module logger rather than with caplog: configure_logging()
    replaces the root handlers wholesale, which drops caplog's.
    """
    import logging

    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    client = _client_raising(RuntimeError("boom"))  # calls configure_logging()
    web_logger = logging.getLogger("common.web")
    handler = _Collect()
    web_logger.addHandler(handler)
    try:
        client.get("/api/v1/groups/team/containers/app", headers={"X-Request-ID": "abc123"})
    finally:
        web_logger.removeHandler(handler)

    record = next(r for r in records if r.levelno == logging.ERROR)
    assert record.request_id == "abc123"
    assert record.exc_info is not None  # the traceback is kept, just not returned


def test_info_publishes_the_internal_code_the_catch_all_can_return():
    """A code a client can receive must be in the advertised vocabulary.

    error_catalog walks subclasses, so the base APIError's INTERNAL/500 - which
    is exactly what the catch-all renders - would otherwise go unpublished.
    """
    codes = dict(
        c["code"] and (c["code"], c["status"])
        for c in TestClient(create_app()).get("/api/v1/containers/info").json()["errorCodes"]
    )
    assert codes["INTERNAL"] == 500
