"""API-managed workload secrets & configs endpoints (docs §7.3)."""

from __future__ import annotations

from fastapi import APIRouter, Response

from app.auth.deps import CurrentUser
from app.dependencies import ResourceDep
from app.models.resource import ResourceCreate, ResourceResponse

router = APIRouter(prefix="/api/v1", tags=["resources"])


# --- secrets -------------------------------------------------------------
@router.post("/secrets", response_model=ResourceResponse, status_code=201)
async def create_secret(
    spec: ResourceCreate, user: CurrentUser, svc: ResourceDep, response: Response
) -> ResourceResponse:
    body, code = await svc.create("secret", spec, user)
    response.status_code = code
    return body


@router.get("/secrets")
async def list_secrets(user: CurrentUser, svc: ResourceDep) -> dict:
    return {"items": await svc.list("secret", user)}


@router.get("/secrets/{name}", response_model=ResourceResponse)
async def get_secret(
    name: str, user: CurrentUser, svc: ResourceDep, reveal: bool = False
) -> ResourceResponse:
    return await svc.get("secret", name, user, reveal=reveal)


@router.delete("/secrets/{name}", status_code=204)
async def delete_secret(name: str, user: CurrentUser, svc: ResourceDep) -> None:
    await svc.delete("secret", name, user)


# --- configs -------------------------------------------------------------
@router.post("/configs", response_model=ResourceResponse, status_code=201)
async def create_config(
    spec: ResourceCreate, user: CurrentUser, svc: ResourceDep, response: Response
) -> ResourceResponse:
    body, code = await svc.create("config", spec, user)
    response.status_code = code
    return body


@router.get("/configs")
async def list_configs(user: CurrentUser, svc: ResourceDep) -> dict:
    return {"items": await svc.list("config", user)}


@router.get("/configs/{name}", response_model=ResourceResponse)
async def get_config(name: str, user: CurrentUser, svc: ResourceDep) -> ResourceResponse:
    return await svc.get("config", name, user)


@router.delete("/configs/{name}", status_code=204)
async def delete_config(name: str, user: CurrentUser, svc: ResourceDep) -> None:
    await svc.delete("config", name, user)
