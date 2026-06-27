"""Container endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, BackgroundTasks

from app.auth.deps import CurrentUser
from app.dependencies import ContainerDep
from app.models.common import WorkloadSummary
from app.models.container import ContainerCreate, ContainerResponse, ContainerUpdate

router = APIRouter(prefix="/api/v1/containers", tags=["containers"])


@router.post("", response_model=ContainerResponse, status_code=202)
async def create_container(
    spec: ContainerCreate,
    user: CurrentUser,
    svc: ContainerDep,
    background: BackgroundTasks,
) -> ContainerResponse:
    """Create a container (202): validate synchronously, deploy in the background.

    Args:
        spec: The container create request.
        user: The authenticated caller (injected).
        svc: The container service (injected).
        background: FastAPI background tasks (injected).

    Returns:
        A Pending response with a ``statusUrl`` to poll for the deploy outcome.
    """
    return await svc.accept(spec, user, background)


@router.put("/{name}", response_model=ContainerResponse, status_code=202)
async def update_container(
    name: str,
    spec: ContainerUpdate,
    user: CurrentUser,
    svc: ContainerDep,
    background: BackgroundTasks,
) -> ContainerResponse:
    """Update a container (202): full replace of the mutable spec, applied async.

    Args:
        name: The workload name.
        spec: The container update request.
        user: The authenticated caller (injected).
        svc: The container service (injected).
        background: FastAPI background tasks (injected).

    Returns:
        A Pending response with a ``statusUrl`` to poll.
    """
    return await svc.accept_update(name, spec, user, background)


@router.get("", response_model=list[WorkloadSummary])
async def list_containers(
    group: str,
    user: CurrentUser,
    svc: ContainerDep,
    sort: Literal["name", "createdAt"] = "name",
) -> list[WorkloadSummary]:
    """List general info for every container the group owns (merged across sites).

    Args:
        group: The owning group.
        user: The authenticated caller (injected).
        svc: The container service (injected).
        sort: Sort key, "name" or "createdAt".

    Returns:
        The per-workload summaries.
    """
    return await svc.list(group, user, sort)


@router.get("/{name}", response_model=ContainerResponse)
async def get_container(
    name: str, group: str, user: CurrentUser, svc: ContainerDep
) -> ContainerResponse:
    """Get one container, including overallStatus and per-site status.

    This is the poll target advertised as ``statusUrl`` on the 202 accept
    response.

    Args:
        name: The workload name.
        group: The owning group.
        user: The authenticated caller (injected).
        svc: The container service (injected).

    Returns:
        The full single-container response.
    """
    return await svc.get(name, group, user)


@router.delete("/{name}", status_code=204)
async def delete_container(
    name: str, group: str, user: CurrentUser, svc: ContainerDep
) -> None:
    """Delete a container and its derived resources (204).

    Args:
        name: The workload name.
        group: The owning group.
        user: The authenticated caller (injected).
        svc: The container service (injected).
    """
    await svc.delete(name, group, user)
