"""Function endpoints."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks

from app.auth.deps import CurrentUser
from app.dependencies import FunctionDep
from app.models.common import WorkloadResponse
from app.models.function import FunctionCreate, FunctionUpdate

router = APIRouter(prefix="/api/v1/functions", tags=["functions"])


@router.post("", response_model=WorkloadResponse, status_code=202)
async def create_function(
    spec: FunctionCreate,
    user: CurrentUser,
    svc: FunctionDep,
    background: BackgroundTasks,
) -> WorkloadResponse:
    # Validated synchronously; deployed in the background. Poll statusUrl.
    return await svc.accept(spec, user, background)


@router.put("/{name}", response_model=WorkloadResponse, status_code=202)
async def update_function(
    name: str,
    spec: FunctionUpdate,
    user: CurrentUser,
    svc: FunctionDep,
    background: BackgroundTasks,
) -> WorkloadResponse:
    return await svc.accept_update(name, spec, user, background)


@router.get("/{name}", response_model=WorkloadResponse)
async def get_function(name: str, user: CurrentUser, svc: FunctionDep) -> WorkloadResponse:
    return await svc.get(name, user)


@router.get("/{name}/status", response_model=WorkloadResponse)
async def function_status(
    name: str, user: CurrentUser, svc: FunctionDep
) -> WorkloadResponse:
    return await svc.get(name, user)


@router.delete("/{name}", status_code=204)
async def delete_function(name: str, user: CurrentUser, svc: FunctionDep) -> None:
    await svc.delete(name, user)
