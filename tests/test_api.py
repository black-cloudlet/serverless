"""API routing tests with auth and services stubbed (no cluster needed)."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.auth.claims import Principal
from app.auth.deps import require_auth
from app.dependencies import get_resource_service, get_workload_service
from app.main import create_app
from app.models.common import WorkloadResponse, ZoneStatus
from app.models.resource import ResourceResponse


class FakeWorkloads:
    async def create_container(self, spec, user):
        return (
            WorkloadResponse(
                name=spec.name,
                type="container",
                url=f"https://{spec.name}-{user.primary_group}.serverless.example.com",
                overallStatus="Ready",
                image=spec.image,
                zones=[ZoneStatus(zone="zone-a", status="Ready")],
                createdAt=datetime.now(timezone.utc),
            ),
            201,
        )

    async def get(self, kind, name, user):
        return WorkloadResponse(
            name=name,
            type=kind,
            url="https://x.serverless.example.com",
            overallStatus="Ready",
            zones=[ZoneStatus(zone="zone-a", status="Ready")],
        )


class FakeResources:
    async def create(self, rtype, spec, user):
        return (
            ResourceResponse(
                name=spec.name,
                type=rtype,
                keys=sorted(spec.data),
                overallStatus="Applied",
                zones=[ZoneStatus(zone="zone-a", status="Applied")],
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


def test_create_container(client):
    r = client.post(
        "/api/v1/containers",
        json={
            "name": "orders-api",
            "image": "registry.internal/team/orders:1",
            "registryUsername": "u",
            "registryToken": "t",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["type"] == "container"
    assert body["url"].startswith("https://orders-api-team.")


def test_create_container_validation_error(client):
    r = client.post("/api/v1/containers", json={"name": "BAD NAME"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_get_function(client):
    r = client.get("/api/v1/functions/foo")
    assert r.status_code == 200
    assert r.json()["name"] == "foo"


def test_create_secret(client):
    r = client.post(
        "/api/v1/secrets", json={"name": "tls", "data": {"tls.key": "abc"}}
    )
    assert r.status_code == 201
    assert r.json()["type"] == "secret"
    assert r.json()["keys"] == ["tls.key"]
