"""Container endpoints."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks

from app.auth.deps import CurrentUser
from app.dependencies import ContainerDep
from app.models.common import WorkloadResponse
from app.models.container import ContainerCreate, ContainerUpdate

router = APIRouter(prefix="/api/v1/containers", tags=["containers"])


@router.post("", response_model=WorkloadResponse, status_code=202)
async def create_container(
    spec: ContainerCreate,
    user: CurrentUser,
    svc: ContainerDep,
    background: BackgroundTasks,
) -> WorkloadResponse:
    # Validated synchronously; deployed in the background. Poll statusUrl.
    return await svc.accept(spec, user, background)


@router.put("/{name}", response_model=WorkloadResponse, status_code=202)
async def update_container(
    name: str,
    spec: ContainerUpdate,
    user: CurrentUser,
    svc: ContainerDep,
    background: BackgroundTasks,
) -> WorkloadResponse:
    return await svc.accept_update(name, spec, user, background)


@router.get("/{name}", response_model=WorkloadResponse)
async def get_container(name: str, user: CurrentUser, svc: ContainerDep) -> WorkloadResponse:
    return await svc.get(name, user)


@router.get("/{name}/status", response_model=WorkloadResponse)
async def container_status(
    name: str, user: CurrentUser, svc: ContainerDep
) -> WorkloadResponse:
    return await svc.get(name, user)


@router.delete("/{name}", status_code=204)
async def delete_container(name: str, user: CurrentUser, svc: ContainerDep) -> None:
    await svc.delete(name, user)
