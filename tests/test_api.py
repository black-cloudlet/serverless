"""API routing tests with auth and services stubbed (no cluster needed)."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.auth.claims import Principal
from app.auth.deps import require_auth
from app.dependencies import get_resource_service, get_workload_service
from app.main import create_app
from app.models.common import WorkloadResponse, SiteStatus
from app.models.resource import ResourceResponse


def _accepted(kind, name, **extra):
    return WorkloadResponse(
        name=name,
        type=kind,
        url=f"https://{name}.serverless.example.com",
        overallStatus="Pending",
        sites=[],
        statusUrl=f"/api/v1/{kind}s/{name}/status",
        **extra,
    )


class FakeWorkloads:
    async def accept_container(self, spec, user, background):
        return _accepted("container", spec.name, image=spec.image)

    async def accept_function(self, spec, user, background):
        return _accepted("function", spec.name, runtime=spec.runtime)

    async def accept_update_container(self, name, spec, user, background):
        return _accepted("container", name, image=spec.image or "kept:1")

    async def accept_update_function(self, name, spec, user, background):
        return _accepted("function", name)

    async def get(self, kind, name, user):
        return WorkloadResponse(
            name=name,
            type=kind,
            url="https://x.serverless.example.com",
            overallStatus="Ready",
            sites=[SiteStatus(site="site-a", status="Ready")],
        )


class FakeResources:
    async def create(self, rtype, spec, user):
        return (
            ResourceResponse(
                name=spec.name,
                type=rtype,
                keys=sorted(spec.data),
                overallStatus="Applied",
                sites=[SiteStatus(site="site-a", status="Applied")],
            ),
            201,
        )


@pytest.fixture
def client():
    app = create_app()
    app.dependency_overrides[require_auth] = lambda: Principal(
        subject="u", username="alice", groups=["team"], is_admin=False
    )
    app.dependency_overrides[get_workload_service] = lambda: FakeWorkloads()
    app.dependency_overrides[get_resource_service] = lambda: FakeResources()
    return TestClient(app)


def test_healthz_no_auth():
    c = TestClient(create_app())
    assert c.get("/healthz").json() == {"status": "ok"}


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
            "image": "registry.internal/team/orders:1",
            "registryUsername": "u",
            "registryToken": "t",
        },
    )
    assert r.status_code == 202
    body = r.json()
    assert body["type"] == "container"
    assert body["overallStatus"] == "Pending"
    assert body["statusUrl"] == "/api/v1/containers/orders-api/status"


def test_create_container_validation_error(client):
    r = client.post("/api/v1/containers", json={"name": "BAD NAME"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_get_function(client):
    r = client.get("/api/v1/functions/foo")
    assert r.status_code == 200
    assert r.json()["name"] == "foo"


def test_update_container_accepted(client):
    r = client.put(
        "/api/v1/containers/orders-api",
        json={"image": "registry.internal/team/orders:2", "scaling": {"minScale": 1}},
    )
    assert r.status_code == 202
    assert r.json()["image"] == "registry.internal/team/orders:2"
    assert r.json()["overallStatus"] == "Pending"


def test_update_function_accepted(client):
    r = client.put("/api/v1/functions/foo", json={"env": [{"name": "X", "value": "1"}]})
    assert r.status_code == 202
    assert r.json()["type"] == "function"


def test_create_secret(client):
    r = client.post(
        "/api/v1/secrets", json={"name": "tls", "data": {"tls.key": "abc"}}
    )
    assert r.status_code == 201
    assert r.json()["type"] == "secret"
    assert r.json()["keys"] == ["tls.key"]
