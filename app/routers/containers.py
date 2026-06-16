"""CaaS endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Response

from app.auth.deps import CurrentUser
from app.dependencies import WorkloadDep
from app.models.common import WorkloadResponse
from app.models.container import ContainerCreate

router = APIRouter(prefix="/api/v1/containers", tags=["containers"])


@router.post("", response_model=WorkloadResponse, status_code=201)
async def create_container(
    spec: ContainerCreate, user: CurrentUser, svc: WorkloadDep, response: Response
) -> WorkloadResponse:
    body, code = await svc.create_container(spec, user)
    response.status_code = code
    return body


@router.get("/{name}", response_model=WorkloadResponse)
async def get_container(name: str, user: CurrentUser, svc: WorkloadDep) -> WorkloadResponse:
    return await svc.get("container", name, user)


@router.get("/{name}/status", response_model=WorkloadResponse)
async def container_status(
    name: str, user: CurrentUser, svc: WorkloadDep
) -> WorkloadResponse:
    return await svc.get("container", name, user)


@router.delete("/{name}", status_code=204)
async def delete_container(name: str, user: CurrentUser, svc: WorkloadDep) -> None:
    await svc.delete("container", name, user)
