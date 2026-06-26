"""Function endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, BackgroundTasks

from app.auth.deps import CurrentUser
from app.dependencies import FunctionDep
from app.models.common import WorkloadSummary
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

    The response carries a ``statusUrl`` to poll for the deploy outcome.
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
    """Update a function (202): full replace of the mutable spec, applied async."""
    return await svc.accept_update(name, spec, user, background)


@router.get("", response_model=list[WorkloadSummary])
async def list_functions(
    group: str,
    user: CurrentUser,
    svc: FunctionDep,
    sort: Literal["name", "createdAt"] = "name",
) -> list[WorkloadSummary]:
    """List general info for every function the group owns (local site)."""
    return await svc.list(group, user, sort)


@router.get("/{name}", response_model=FunctionResponse)
async def get_function(
    name: str, group: str, user: CurrentUser, svc: FunctionDep
) -> FunctionResponse:
    """Get one function, including overallStatus and per-site status.

    This is the poll target advertised as ``statusUrl`` on the 202 accept
    response.
    """
    return await svc.get(name, group, user)


@router.delete("/{name}", status_code=204)
async def delete_function(
    name: str, group: str, user: CurrentUser, svc: FunctionDep
) -> None:
    """Delete a function and its derived resources (204)."""
    await svc.delete(name, group, user)
