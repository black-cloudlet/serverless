"""CaaS endpoints."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks

from app.auth.deps import CurrentUser
from app.dependencies import WorkloadDep
from app.models.common import WorkloadResponse
from app.models.container import ContainerCreate, ContainerUpdate

router = APIRouter(prefix="/api/v1/containers", tags=["containers"])


@router.post("", response_model=WorkloadResponse, status_code=202)
async def create_container(
    spec: ContainerCreate,
    user: CurrentUser,
    svc: WorkloadDep,
    background: BackgroundTasks,
) -> WorkloadResponse:
    # Validated synchronously; deployed in the background. Poll statusUrl.
    return await svc.accept_container(spec, user, background)


@router.put("/{name}", response_model=WorkloadResponse, status_code=202)
async def update_container(
    name: str,
    spec: ContainerUpdate,
    user: CurrentUser,
    svc: WorkloadDep,
    background: BackgroundTasks,
) -> WorkloadResponse:
    return await svc.accept_update_container(name, spec, user, background)


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
