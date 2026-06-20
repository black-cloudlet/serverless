"""API routing tests with auth and services stubbed (no cluster needed)."""

import pytest
from fastapi.testclient import TestClient

from app.auth.claims import Principal
from app.auth.deps import require_auth
from app.dependencies import get_container_service, get_function_service
from app.main import create_app
from app.models.common import ContainerResponse, FunctionResponse, SiteStatus


def _model(kind, **fields):
    cls = FunctionResponse if kind == "function" else ContainerResponse
    return cls(**fields)


def _accepted(kind, name, group, **extra):
    return _model(
        kind,
        name=name,
        type=kind,
        url=f"https://{name}.serverless.example.com",
        overallStatus="Pending",
        sites=[],
        statusUrl=f"/api/v1/{kind}s/{name}?group={group}",
        **extra,
    )


def _ready(kind, name, **extra):
    return _model(
        kind,
        name=name,
        type=kind,
        url="https://x.serverless.example.com",
        overallStatus="Ready",
        sites=[SiteStatus(site="site-a", status="Ready")],
        **extra,
    )


class FakeFunctions:
    async def accept(self, spec, user, background):
        return _accepted("function", spec.name, spec.group, runtime=spec.runtime)

    async def accept_update(self, name, spec, user, background):
        return _accepted("function", name, spec.group)

    async def get(self, name, group, user):
        return _ready("function", name, runtime="python", gitRepo="https://git/x.git", branch="main")

    async def list(self, group, user):
        from app.models.common import WorkloadSummary

        return [
            WorkloadSummary(
                name="fn-a", type="function", url="https://fn-a.example.com",
                overallStatus="Ready", size="small", sites=["central"],
            )
        ]

    async def delete(self, name, group, user):
        return None


class FakeContainers:
    async def accept(self, spec, user, background):
        return _accepted("container", spec.name, spec.group, image=spec.image)

    async def accept_update(self, name, spec, user, background):
        return _accepted("container", name, spec.group, image=spec.image or "kept:1")

    async def get(self, name, group, user):
        return _ready("container", name, image="reg/x:1", registryUsername="svc-team")

    async def list(self, group, user):
        from app.models.common import WorkloadSummary

        return [
            WorkloadSummary(
                name="ctr-a", type="container", url="https://ctr-a.example.com",
                overallStatus="Ready", size="medium", sites=["central", "south"],
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


async def test_startup_warmup_is_best_effort(monkeypatch):
    """A failing OIDC discovery / cluster connect must not crash startup."""
    from app.core.config import Settings, SiteConfig, SSOConfig
    from app.main import _warmup
    from app.services.deployer import Deployer

    class _BoomValidator:
        def warmup(self):
            raise RuntimeError("sso down")

    monkeypatch.setattr("app.main.get_validator", lambda: _BoomValidator())

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
    from app.core.config import get_settings

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
        "/api/v1/containers",
        json={
            "name": "orders-api",
            "group": "team",
            "image": "registry.internal/team/orders:1",
            "registryUsername": "u",
            "registryToken": "t",
        },
    )
    assert r.status_code == 202
    body = r.json()
    assert body["type"] == "container"
    assert body["overallStatus"] == "Pending"
    assert body["statusUrl"] == "/api/v1/containers/orders-api?group=team"


def test_create_container_validation_error(client):
    r = client.post("/api/v1/containers", json={"name": "BAD NAME"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_get_function(client):
    r = client.get("/api/v1/functions/foo?group=team")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "foo"
    # FunctionResponse is flat and mirrors the create body: function source fields
    # are present, container-only fields are absent.
    assert body["runtime"] == "python" and body["gitRepo"] == "https://git/x.git"
    # the built image is internal to functions; not exposed
    assert "registryUsername" not in body
    assert "image" not in body and "imageDigest" not in body


def test_get_container_shape(client):
    r = client.get("/api/v1/containers/foo?group=team")
    assert r.status_code == 200
    body = r.json()
    # ContainerResponse mirrors the create body: image + registryUsername present,
    # function-only fields (gitRepo/runtime) absent.
    assert body["image"] == "reg/x:1" and body["registryUsername"] == "svc-team"
    assert "gitRepo" not in body and "runtime" not in body


def test_get_function_requires_group(client):
    r = client.get("/api/v1/functions/foo")  # missing ?group=
    assert r.status_code == 400


def test_list_functions(client):
    r = client.get("/api/v1/functions?group=team")
    assert r.status_code == 200
    body = r.json()
    assert [w["name"] for w in body] == ["fn-a"]
    assert body[0]["type"] == "function" and body[0]["sites"] == ["central"]


def test_list_containers(client):
    r = client.get("/api/v1/containers?group=team")
    assert r.status_code == 200
    body = r.json()
    assert body[0]["name"] == "ctr-a"
    assert body[0]["size"] == "medium" and body[0]["sites"] == ["central", "south"]


def test_list_requires_group(client):
    r = client.get("/api/v1/containers")  # missing ?group=
    assert r.status_code == 400


def test_update_container_accepted(client):
    r = client.put(
        "/api/v1/containers/orders-api",
        json={
            "group": "team",
            "image": "registry.internal/team/orders:2",
            "scaling": {"minScale": 1},
        },
    )
    assert r.status_code == 202
    assert r.json()["image"] == "registry.internal/team/orders:2"
    assert r.json()["overallStatus"] == "Pending"


def test_update_function_accepted(client):
    r = client.put(
        "/api/v1/functions/foo",
        json={"group": "team", "env": [{"name": "X", "value": "1"}]},
    )
    assert r.status_code == 202
    assert r.json()["type"] == "function"


def test_update_container_partial_creds_rejected(client):
    r = client.put(
        "/api/v1/containers/orders-api",
        json={"group": "team", "registryUsername": "bob"},  # token missing
    )
    assert r.status_code == 400


def test_update_function_build_change_requires_token(client):
    r = client.put(
        "/api/v1/functions/foo",
        json={"group": "team", "branch": "release"},  # gitToken missing
    )
    assert r.status_code == 400
