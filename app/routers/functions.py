"""FaaS endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Response

from app.auth.deps import CurrentUser
from app.dependencies import WorkloadDep
from app.models.common import WorkloadResponse
from app.models.function import FunctionCreate

router = APIRouter(prefix="/api/v1/functions", tags=["functions"])


@router.post("", response_model=WorkloadResponse, status_code=201)
async def create_function(
    spec: FunctionCreate, user: CurrentUser, svc: WorkloadDep, response: Response
) -> WorkloadResponse:
    body, code = await svc.create_function(spec, user)
    response.status_code = code
    return body


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
