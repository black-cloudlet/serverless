"""Function endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, BackgroundTasks

from app.auth.deps import CurrentUser
from app.dependencies import FunctionDep
from app.models.common import Group, WorkloadSummary
from app.models.function import FunctionCreate, FunctionResponse, FunctionUpdate

router = APIRouter(prefix="/api/v1/functions", tags=["functions"])


@router.post("", response_model=FunctionResponse, status_code=202)
async def create_function(
    spec: FunctionCreate,
    user: CurrentUser,
    svc: FunctionDep,
    background: BackgroundTasks,
) -> FunctionResponse:
    """Create a function (202): validate synchronously, build and deploy async.

    Args:
        spec: The function create request.
        user: The authenticated caller (injected).
        svc: The function service (injected).
        background: FastAPI background tasks (injected).

    Returns:
        A Pending response with a ``statusUrl`` to poll for the deploy outcome.
    """
    return await svc.accept(spec, user, background)


@router.put("/{name}", response_model=FunctionResponse, status_code=202)
async def update_function(
    name: str,
    spec: FunctionUpdate,
    user: CurrentUser,
    svc: FunctionDep,
    background: BackgroundTasks,
) -> FunctionResponse:
    """Update a function (202): full replace of the mutable spec, applied async.

    Args:
        name: The workload name.
        spec: The function update request.
        user: The authenticated caller (injected).
        svc: The function service (injected).
        background: FastAPI background tasks (injected).

    Returns:
        A Pending response with a ``statusUrl`` to poll.
    """
    return await svc.accept_update(name, spec, user, background)


@router.get("", response_model=list[WorkloadSummary])
async def list_functions(
    group: Group,
    user: CurrentUser,
    svc: FunctionDep,
    sort: Literal["name", "createdAt"] = "name",
) -> list[WorkloadSummary]:
    """List general info for every function the group owns (merged across sites).

    Args:
        group: The owning group.
        user: The authenticated caller (injected).
        svc: The function service (injected).
        sort: Sort key, "name" or "createdAt".

    Returns:
        The per-workload summaries.
    """
    return await svc.list(group, user, sort)


@router.get("/{name}", response_model=FunctionResponse)
async def get_function(
    name: str, group: Group, user: CurrentUser, svc: FunctionDep
) -> FunctionResponse:
    """Get one function, including overallStatus and per-site status.

    This is the poll target advertised as ``statusUrl`` on the 202 accept
    response.

    Args:
        name: The workload name.
        group: The owning group.
        user: The authenticated caller (injected).
        svc: The function service (injected).

    Returns:
        The full single-function response.
    """
    return await svc.get(name, group, user)


@router.delete("/{name}", status_code=204)
async def delete_function(
    name: str, group: Group, user: CurrentUser, svc: FunctionDep
) -> None:
    """Delete a function and its derived resources (204).

    Args:
        name: The workload name.
        group: The owning group.
        user: The authenticated caller (injected).
        svc: The function service (injected).
    """
    await svc.delete(name, group, user)
