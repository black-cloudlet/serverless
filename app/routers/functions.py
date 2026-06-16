"""FaaS endpoints."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks

from app.auth.deps import CurrentUser
from app.dependencies import WorkloadDep
from app.models.common import WorkloadResponse
from app.models.function import FunctionCreate, FunctionUpdate

router = APIRouter(prefix="/api/v1/functions", tags=["functions"])


@router.post("", response_model=WorkloadResponse, status_code=202)
async def create_function(
    spec: FunctionCreate,
    user: CurrentUser,
    svc: WorkloadDep,
    background: BackgroundTasks,
) -> WorkloadResponse:
    # Validated synchronously; deployed in the background. Poll statusUrl.
    return await svc.accept_function(spec, user, background)


@router.put("/{name}", response_model=WorkloadResponse, status_code=202)
async def update_function(
    name: str,
    spec: FunctionUpdate,
    user: CurrentUser,
    svc: WorkloadDep,
    background: BackgroundTasks,
) -> WorkloadResponse:
    return await svc.accept_update_function(name, spec, user, background)


@router.get("/{name}", response_model=WorkloadResponse)
async def get_function(name: str, user: CurrentUser, svc: WorkloadDep) -> WorkloadResponse:
    return await svc.get("function", name, user)


@router.get("/{name}/status", response_model=WorkloadResponse)
async def function_status(
    name: str, user: CurrentUser, svc: WorkloadDep
) -> WorkloadResponse:
    return await svc.get("function", name, user)


@router.delete("/{name}", status_code=204)
async def delete_function(name: str, user: CurrentUser, svc: WorkloadDep) -> None:
    await svc.delete("function", name, user)
