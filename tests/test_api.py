"""API routing tests with auth and services stubbed (no cluster needed)."""

import pytest
from cloudlet_apis.auth import Principal
from fastapi.testclient import TestClient

from api.auth.deps import require_auth
from api.dependencies import get_container_service, get_function_service
from api.main import create_app
from api.models.common import (
    RegionStats,
    RegionStatus,
    ResourceUsage,
    WorkloadStatsResponse,
)
from api.models.container import ContainerResponse
from api.models.function import FunctionResponse


def _model(kind, **fields):
    cls = FunctionResponse if kind == "function" else ContainerResponse
    return cls(**fields)


def _stats(overall="Ready"):
    """A live stats view: two replicas at one region."""
    return WorkloadStatsResponse(
        status=overall,
        replicas=2,
        usage=ResourceUsage(cpu="300m", memory="384Mi"),
        regions=[
            RegionStats(
                region="region-a",
                status=overall,
                replicas=2,
                usage=ResourceUsage(cpu="300m", memory="384Mi"),
            )
        ],
    )


def _accepted(kind, name, group, **extra):
    return _model(
        kind,
        name=name,
        group=group,
        type=kind,
        hostname=f"{name}.serverless.example.com",
        status="Pending",
        regions=[],
        statusUrl=f"/v1/groups/{group}/{kind}s/{name}",
        **extra,
    )


def _ready(kind, name, group="team", **extra):
    return _model(
        kind,
        name=name,
        group=group,
        type=kind,
        hostname="x.serverless.example.com",
        status="Ready",
        regions=[RegionStatus(region="region-a", status="Ready")],
        **extra,
    )


class FakeFunctions:
    async def accept(self, group, spec, user, background):
        return _accepted("function", spec.name, group, runtime=spec.runtime)

    async def accept_update(self, group, name, spec, user, background):
        return _accepted("function", name, group)

    async def accept_build(self, group, name, user, background):
        return _accepted("function", name, group, runtime="python", branch="main")

    async def get(self, name, group, user):
        return _ready(
            "function", name, runtime="python", gitRepo="https://git/x.git", branch="main"
        )

    async def stats(self, name, group, user):
        return _stats("Building")

    async def list(self, group, user, sort="name"):
        from api.models.common import WorkloadSummary

        return [
            WorkloadSummary(
                name="fn-a",
                group="team",
                type="function",
                hostname="fn-a.example.com",
                status="Ready",
                size="small",
                regions=["central"],
            )
        ]

    async def delete(self, name, group, user):
        return None


class FakeContainers:
    async def accept(self, group, spec, user, background):
        return _accepted("container", spec.name, group, image=spec.image)

    async def accept_update(self, group, name, spec, user, background):
        return _accepted("container", name, group, image=spec.image or "kept:1")

    async def accept_pull(self, group, name, user, background):
        return _accepted("container", name, group, image="reg/x:1")

    async def get(self, name, group, user):
        return _ready("container", name, image="reg/x:1", registryUsername="svc")

    async def stats(self, name, group, user):
        return _stats()

    async def list(self, group, user, sort="name"):
        from api.models.common import WorkloadSummary

        return [
            WorkloadSummary(
                name="ctr-a",
                group="team",
                type="container",
                hostname="ctr-a.example.com",
                status="Ready",
                size="medium",
                regions=["central", "south"],
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
    for path in ("/v1/containers/info", "/v1/functions/info"):
        body = c.get(path).json()
        assert body["version"]
        assert isinstance(body["regions"], list)
        assert body["sizes"] == ["small", "medium", "large"]
        assert body["routeDomain"]
        assert body["defaultHostTemplate"] == "{name}-{group}.{routeDomain}"

    # the port rules are identical for both offerings, and both publish them
    rules = {"required": False, "default": 8080, "min": 1, "max": 65535}
    cont = c.get("/v1/containers/info").json()
    fn = c.get("/v1/functions/info").json()
    assert cont["port"] == rules
    assert fn["port"] == rules

    # ...only the offering-specific halves differ
    assert "runtimes" not in cont
    assert "python" in [r["name"] for r in fn["runtimes"]]
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
    from api.core.config import RegionConfig, Settings, SSOConfig
    from api.main import _warmup
    from api.services.regions.deployer import Deployer

    class _BoomValidator:
        def warmup(self):
            raise RuntimeError("sso down")

    monkeypatch.setattr("api.main.get_auth", lambda: _BoomValidator())

    settings = Settings(
        auth_enabled=True,
        sso=SSOConfig(),
        regions=[RegionConfig(name="region-a", cluster="region-a-0")],
        cluster_connect_timeout=0.01,
        cluster_read_timeout=0.01,
    )

    class _BoomCluster:
        region = "region-a"

        def connect(self):
            raise RuntimeError("cluster unreachable")

    deployer = Deployer(settings)
    deployer._clusters = {"region-a": _BoomCluster()}
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
        "/v1/groups/team/containers",
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
    assert body["status"] == "Pending"
    assert body["statusUrl"] == "/v1/groups/team/containers/orders-api"


def test_build_function_accepted_without_a_body(client):
    """A rebuild carries no inputs: the ones to build with are already stored."""
    r = client.post("/v1/groups/team/functions/orders/build")

    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "Pending"
    # the same poll target as create/update, so a client needs no second flow
    assert body["statusUrl"] == "/v1/groups/team/functions/orders"
    # the build inputs it will use, echoed back from what is stored
    assert body["runtime"] == "python" and body["branch"] == "main"


def test_only_functions_can_be_built(client):
    """A container is deployed from an image the caller built; there is nothing to build."""
    assert client.post("/v1/groups/team/containers/orders/build").status_code == 404


def test_build_path_name_validated_at_the_edge(client):
    assert client.post("/v1/groups/team/functions/Bad_Name/build").status_code == 400


def test_pull_container_accepted_without_a_body(client):
    """Nothing to send: the image to re-resolve is the one already deployed."""
    r = client.post("/v1/groups/team/containers/orders-api/pull")

    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "Pending"
    assert body["statusUrl"] == "/v1/groups/team/containers/orders-api"


def test_only_containers_can_be_pulled(client):
    """A function's digest is the build controller's to roll out, not a re-pull."""
    assert client.post("/v1/groups/team/functions/orders/pull").status_code == 404


def test_pull_path_name_validated_at_the_edge(client):
    assert client.post("/v1/groups/team/containers/Bad_Name/pull").status_code == 400


def test_create_container_validation_error(client):
    r = client.post("/v1/groups/team/containers", json={"name": "BAD NAME"})
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
    runtimes = {r["name"]: r for r in c.get("/v1/functions/info").json()["runtimes"]}

    assert runtimes["python"]["versions"] == ["3.11", "3.12"]
    assert runtimes["python"]["defaultVersion"] == "3.12"
    assert runtimes["node"]["versions"] == ["18", "20", "22"]


def test_info_publishes_the_status_and_error_vocabularies():
    """Everything a client would otherwise hardcode and let drift."""
    from typing import get_args

    from api.models.common import REGION_STATUSES, WorkloadStatus
    from common.errors import ValidationError, error_catalog

    body = TestClient(create_app()).get("/v1/containers/info").json()

    # derived from the Literal the responses are typed with, not a second list
    assert body["statuses"]["workload"] == list(get_args(WorkloadStatus))
    assert body["statuses"]["region"] == list(REGION_STATUSES)
    # a poller needs to know which values mean "stop"
    assert set(body["statuses"]["terminal"]) < set(body["statuses"]["workload"])
    assert "Building" not in body["statuses"]["terminal"]

    codes = {e["code"]: e["status"] for e in body["errorCodes"]}
    assert codes[ValidationError.code] == ValidationError.status_code
    assert len(codes) == len(error_catalog())


def test_info_publishes_the_combined_name_and_group_limit():
    """No per-field schema can carry this, so /info has to.

    Each half may be 63 characters on its own; it is the join that becomes the
    default host's first label. A form validating the fields separately would
    not see the pair - but the rule binds only the default host, so a client
    should treat it as "supply a hostname", not as a hard reject.
    """
    from common.names import MAX_HOST_LABEL, default_host_label

    naming = TestClient(create_app()).get("/v1/containers/info").json()["naming"]

    # composed by the same function the platform names objects with
    assert naming["template"] == default_host_label("{name}", "{group}")
    assert naming["maxLength"] == MAX_HOST_LABEL
    # the pair the rule exists to catch: both halves legal, the join is not
    assert len("n" * 40) <= 63 and len("g" * 40) <= 63
    assert len(default_host_label("n" * 40, "g" * 40)) > naming["maxLength"]


def test_our_own_error_is_published_in_the_catalog():
    """RegionTotalFailure is defined in this repository, not the shared package.

    error_catalog walks subclasses at call time, which is what lets us keep a
    platform-specific error locally and still have /info advertise it. The walk
    itself is cloudlet_apis.errors' behaviour and is tested there; this is the
    part that would break silently if RegionTotalFailure stopped being imported.
    """
    from common.errors import RegionTotalFailure, error_catalog

    codes = dict(error_catalog())
    assert codes[RegionTotalFailure.code] == RegionTotalFailure.status_code

    body = TestClient(create_app()).get("/v1/containers/info").json()
    assert {"code": "REGION_TOTAL_FAILURE", "status": 502} in body["errorCodes"]


def test_framework_http_errors_get_a_meaningful_code_and_status(client):
    # Unknown route / wrong method are framework HTTP errors (not domain errors);
    # the code is derived from the status, not a flat "HTTP_ERROR".
    r = client.get("/v1/does-not-exist")
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["status"] == 404 and err["code"] == "NOT_FOUND"

    # POST to a path that only serves GET/PUT/DELETE -> 405
    r = client.post("/v1/groups/team/functions/foo")
    assert r.status_code == 405
    err = r.json()["error"]
    assert err["status"] == 405 and err["code"] == "METHOD_NOT_ALLOWED"


def test_get_function(client):
    r = client.get("/v1/groups/team/functions/foo")
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
    r = client.get("/v1/groups/team/containers/foo")
    assert r.status_code == 200
    body = r.json()
    # ContainerResponse mirrors the create body: image + registryUsername present,
    # function-only fields (gitRepo/runtime) absent.
    assert body["image"] == "reg/x:1" and body["registryUsername"] == "svc"
    assert "gitRepo" not in body and "runtime" not in body


def test_get_container_stats_is_live_state_only(client):
    r = client.get("/v1/groups/team/containers/foo/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "Ready"
    assert body["replicas"] == 2
    assert body["usage"] == {"cpu": "300m", "memory": "384Mi"}
    region = body["regions"][0]
    assert region == {
        "region": "region-a",
        "status": "Ready",
        "reason": None,
        "replicas": 2,
        "usage": {"cpu": "300m", "memory": "384Mi"},
    }
    # nothing else: no desired-state config, and no identity echo of the path
    assert set(body) == {"status", "reason", "replicas", "usage", "regions"}


def test_get_function_stats_reports_a_running_build(client):
    # Building comes from the build read, which stays even though it is not a field
    body = client.get("/v1/groups/team/functions/foo/stats").json()
    assert body["status"] == "Building"
    assert body["regions"][0]["status"] == "Building"


def test_stats_path_name_validated_at_the_edge(client):
    assert client.get("/v1/groups/team/functions/Bad_Name/stats").status_code == 400


def test_path_name_validated_at_the_edge(client):
    """A path {name} that isn't a DNS-1123 label is rejected at the boundary (400),
    like the request-body name - not passed through to a cluster lookup."""
    assert client.get("/v1/groups/team/functions/Bad_Name").status_code == 400
    assert client.delete("/v1/groups/team/containers/UPPER").status_code == 400


def test_list_functions(client):
    r = client.get("/v1/groups/team/functions")
    assert r.status_code == 200
    body = r.json()
    assert [w["name"] for w in body] == ["fn-a"]
    assert body[0]["type"] == "function" and body[0]["group"] == "team"
    assert body[0]["regions"] == ["central"]


def test_list_containers(client):
    r = client.get("/v1/groups/team/containers")
    assert r.status_code == 200
    body = r.json()
    assert body[0]["name"] == "ctr-a"
    assert body[0]["size"] == "medium" and body[0]["regions"] == ["central", "south"]


def test_list_accepts_sort_and_rejects_unknown(client):
    assert client.get("/v1/groups/team/functions?sort=createdAt").status_code == 200
    assert client.get("/v1/groups/team/functions?sort=name").status_code == 200
    assert client.get("/v1/groups/team/functions?sort=bogus").status_code == 400


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

    r = c.get("/v1/groups/ggd-1234-platforms/functions/foo")
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
        r = c.get(f"/v1/groups/{path_group}/functions/foo")
        assert r.status_code == 200
    assert seen == ["my-team"] * 4


def test_update_container_accepted(client):
    r = client.put(
        "/v1/groups/team/containers/orders-api",
        json={
            "image": "registry.internal/team/orders:2",
            "port": 8080,
            "scaling": {"minScale": 1},
        },
    )
    assert r.status_code == 202
    assert r.json()["image"] == "registry.internal/team/orders:2"
    assert r.json()["status"] == "Pending"


def test_update_function_accepted(client):
    r = client.put(
        "/v1/groups/team/functions/foo",
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
        "/v1/groups/team/containers/orders-api",
        json={
            "image": "registry.internal/team/orders:2",
            "port": 8080,
            "registryUsername": "bob",  # token omitted -> keep
        },
    )
    assert r.status_code == 202


def test_update_container_token_without_username_rejected(client):
    r = client.put(
        "/v1/groups/team/containers/orders-api",
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
        "/v1/groups/team/functions/foo",
        json={
            "gitRepo": "https://git/x.git",
            "runtime": "python",
            "branch": "release",  # gitToken omitted -> reuse stored token
        },
    )
    assert r.status_code == 202


def test_docs_are_served_from_local_assets_not_a_cdn():
    """The app must build its docs offline - unreachable CDN, airgapped cluster.

    That the vendored assets exist and are served is cloudlet_apis.web's job and
    is tested there. What is ours is calling mount_offline_docs at all, and
    building the app with docs_url/redoc_url disabled so it takes effect.
    """
    client = TestClient(create_app())

    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "/static/swagger-ui-bundle.js" in docs.text
    assert "cdn.jsdelivr.net" not in docs.text

    redoc = client.get("/redoc")
    assert redoc.status_code == 200
    assert "cdn.jsdelivr.net" not in redoc.text


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


def test_a_swagger_client_secret_moves_the_token_exchange_server_side(monkeypatch):
    """Where the SSO realm forbids PUBLIC clients (docs/ARCHITECTURE.md).

    Set the secret and the token leg is proxied through this API, so the
    Keycloak client can be registered confidential. The browser still authorizes
    against SSO with PKCE, and the secret must not reach it.
    """
    from api.core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("SERVERLESS_SSO__SWAGGER_CLIENT_SECRET", "from-vault")
    try:
        app = create_app()
        flow = app.openapi()["components"]["securitySchemes"]["SSO"]["flows"]["authorizationCode"]

        assert flow["tokenUrl"] == "/auth/token"  # ...this API, not Keycloak
        assert flow["authorizationUrl"].endswith("/protocol/openid-connect/auth")
        # The secret stays in the pod: not in the served schema, not in the
        # Swagger bootstrap, and PKCE still on.
        assert "from-vault" not in str(app.openapi())
        assert "from-vault" not in str(app.swagger_ui_init_oauth)
        assert app.swagger_ui_init_oauth["usePkceWithAuthorizationCodeGrant"] is True
        # Mounted, and hidden from the published schema.
        assert "/auth/token" not in app.openapi().get("paths", {})
        assert (
            TestClient(app)
            .post("/auth/token", data={"grant_type": "client_credentials"})
            .status_code
            == 400  # only an interactive login is ever completed
        )
    finally:
        get_settings.cache_clear()


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


def test_info_publishes_the_internal_code_the_catch_all_can_return():
    """A code a client can receive must be in the advertised vocabulary.

    error_catalog walks subclasses, so the base APIError's INTERNAL/500 - which
    is exactly what the catch-all renders - would otherwise go unpublished.
    """
    codes = dict(
        c["code"] and (c["code"], c["status"])
        for c in TestClient(create_app()).get("/v1/containers/info").json()["errorCodes"]
    )
    assert codes["INTERNAL"] == 500
