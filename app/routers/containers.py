"""Container endpoints."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks

from app.auth.deps import CurrentUser
from app.dependencies import ContainerDep
from app.models.common import WorkloadResponse, WorkloadSummary
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


@router.get("", response_model=list[WorkloadSummary])
async def list_containers(
    group: str, user: CurrentUser, svc: ContainerDep
) -> list[WorkloadSummary]:
    # General info for every container the group owns, merged across sites.
    return await svc.list(group, user)


@router.get("/{name}", response_model=WorkloadResponse)
async def get_container(
    name: str, group: str, user: CurrentUser, svc: ContainerDep
) -> WorkloadResponse:
    # Also the poll target advertised as `statusUrl` on the 202 accept response;
    # the response already carries overallStatus + per-site status.
    return await svc.get(name, group, user)


@router.delete("/{name}", status_code=204)
async def delete_container(
    name: str, group: str, user: CurrentUser, svc: ContainerDep
) -> None:
    await svc.delete(name, group, user)
