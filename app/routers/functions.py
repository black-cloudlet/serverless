"""Function endpoints."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks

from app.auth.deps import CurrentUser
from app.dependencies import FunctionDep
from app.models.common import WorkloadResponse, WorkloadSummary
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


@router.get("", response_model=list[WorkloadSummary])
async def list_functions(
    group: str, user: CurrentUser, svc: FunctionDep
) -> list[WorkloadSummary]:
    # General info for every function the group owns, merged across sites.
    return await svc.list(group, user)


@router.get("/{name}", response_model=WorkloadResponse)
async def get_function(
    name: str, group: str, user: CurrentUser, svc: FunctionDep
) -> WorkloadResponse:
    # Also the poll target advertised as `statusUrl` on the 202 accept response;
    # the response already carries overallStatus + per-site status.
    return await svc.get(name, group, user)


@router.delete("/{name}", status_code=204)
async def delete_function(
    name: str, group: str, user: CurrentUser, svc: FunctionDep
) -> None:
    await svc.delete(name, group, user)
