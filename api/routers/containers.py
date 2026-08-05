"""Container endpoints."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Query

from api.auth.deps import CurrentUser
from api.dependencies import ContainerDep
from api.models.common import (
    Group,
    LogsResponse,
    Name,
    WorkloadStatsResponse,
    WorkloadSummary,
)
from api.models.container import ContainerCreate, ContainerResponse, ContainerUpdate

router = APIRouter(prefix="/api/v1/groups/{group}/containers", tags=["containers"])


@router.post("", response_model=ContainerResponse, status_code=202)
async def create_container(
    group: Group,
    spec: ContainerCreate,
    user: CurrentUser,
    svc: ContainerDep,
    background: BackgroundTasks,
) -> ContainerResponse:
    """Create a container (202): validate synchronously, deploy in the background."""
    return await svc.accept(group, spec, user, background)


@router.put("/{name}", response_model=ContainerResponse, status_code=202)
async def update_container(
    group: Group,
    name: Name,
    spec: ContainerUpdate,
    user: CurrentUser,
    svc: ContainerDep,
    background: BackgroundTasks,
) -> ContainerResponse:
    """Update a container (202): full replace of the mutable spec, applied async."""
    return await svc.accept_update(group, name, spec, user, background)


@router.get("", response_model=list[WorkloadSummary])
async def list_containers(
    group: Group,
    user: CurrentUser,
    svc: ContainerDep,
    sort: Literal["name", "createdAt"] = "name",
) -> list[WorkloadSummary]:
    """List general info for every container the group owns (merged across sites)."""
    return await svc.list(group, user, sort)


@router.get("/{name}", response_model=ContainerResponse)
async def get_container(
    group: Group, name: Name, user: CurrentUser, svc: ContainerDep
) -> ContainerResponse:
    """Get one container, including overallStatus and per-site status.

    This is the poll target advertised as ``statusUrl`` on the 202 accept
    response.
    """
    return await svc.get(name, group, user)


@router.get("/{name}/stats", response_model=WorkloadStatsResponse)
async def get_container_stats(
    group: Group, name: Name, user: CurrentUser, svc: ContainerDep
) -> WorkloadStatsResponse:
    """Get the container's live state: status, replicas and usage, per site.

    The lightweight endpoint to poll - the same ``overallStatus`` as the full GET
    and the live numbers behind it, and none of the desired-state config.
    """
    return await svc.stats(name, group, user)


@router.get("/{name}/logs", response_model=LogsResponse)
async def get_container_logs(
    group: Group,
    name: Name,
    user: CurrentUser,
    svc: ContainerDep,
    container: str = "user-container",
    sinceSeconds: Annotated[int | None, Query(gt=0)] = None,
    limitBytes: Annotated[int | None, Query(gt=0)] = None,
) -> LogsResponse:
    """Snapshot the container's pod logs from the current site.

    Point-in-time (not streamed) and local-site only; a scaled-to-zero workload
    returns no pods.
    """
    return await svc.logs(
        name, group, user, container=container, since_seconds=sinceSeconds, limit_bytes=limitBytes
    )


@router.delete("/{name}", status_code=204)
async def delete_container(group: Group, name: Name, user: CurrentUser, svc: ContainerDep) -> None:
    """Delete a container and its derived resources (204)."""
    await svc.delete(name, group, user)
