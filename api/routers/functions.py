"""Function endpoints."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Query

from api.auth.deps import CurrentUser
from api.dependencies import FunctionDep
from api.models.common import Group, LogsResponse, Name, WorkloadSummary
from api.models.function import FunctionCreate, FunctionResponse, FunctionUpdate

router = APIRouter(prefix="/api/v1/groups/{group}/functions", tags=["functions"])


@router.post("", response_model=FunctionResponse, status_code=202)
async def create_function(
    group: Group,
    spec: FunctionCreate,
    user: CurrentUser,
    svc: FunctionDep,
    background: BackgroundTasks,
) -> FunctionResponse:
    """Create a function (202): validate synchronously, build and deploy async.

    Args:
        group: The owning group (from the request path).
        spec: The function create request.
        user: The authenticated caller (injected).
        svc: The function service (injected).
        background: FastAPI background tasks (injected).

    Returns:
        A Pending response with a ``statusUrl`` to poll for the deploy outcome.
    """
    return await svc.accept(group, spec, user, background)


@router.put("/{name}", response_model=FunctionResponse, status_code=202)
async def update_function(
    group: Group,
    name: Name,
    spec: FunctionUpdate,
    user: CurrentUser,
    svc: FunctionDep,
    background: BackgroundTasks,
) -> FunctionResponse:
    """Update a function (202): full replace of the mutable spec, applied async.

    Args:
        group: The owning group (from the request path).
        name: The workload name.
        spec: The function update request.
        user: The authenticated caller (injected).
        svc: The function service (injected).
        background: FastAPI background tasks (injected).

    Returns:
        A Pending response with a ``statusUrl`` to poll.
    """
    return await svc.accept_update(group, name, spec, user, background)


@router.get("", response_model=list[WorkloadSummary])
async def list_functions(
    group: Group,
    user: CurrentUser,
    svc: FunctionDep,
    sort: Literal["name", "createdAt"] = "name",
) -> list[WorkloadSummary]:
    """List general info for every function the group owns (merged across sites).

    Args:
        group: The owning group (from the request path).
        user: The authenticated caller (injected).
        svc: The function service (injected).
        sort: Sort key, "name" or "createdAt".

    Returns:
        The per-workload summaries.
    """
    return await svc.list(group, user, sort)


@router.get("/{name}", response_model=FunctionResponse)
async def get_function(
    group: Group, name: Name, user: CurrentUser, svc: FunctionDep
) -> FunctionResponse:
    """Get one function, including overallStatus and per-site status.

    This is the poll target advertised as ``statusUrl`` on the 202 accept
    response.

    Args:
        group: The owning group (from the request path).
        name: The workload name.
        user: The authenticated caller (injected).
        svc: The function service (injected).

    Returns:
        The full single-function response.
    """
    return await svc.get(name, group, user)


@router.get("/{name}/logs", response_model=LogsResponse)
async def get_function_logs(
    group: Group,
    name: Name,
    user: CurrentUser,
    svc: FunctionDep,
    container: str = "user-container",
    sinceSeconds: Annotated[int | None, Query(gt=0)] = None,
    limitBytes: Annotated[int | None, Query(gt=0)] = None,
) -> LogsResponse:
    """Snapshot the function's pod logs from the current site.

    Point-in-time (not streamed) and local-site only; a scaled-to-zero workload
    returns no pods.

    Args:
        group: The owning group (from the request path).
        name: The workload name.
        user: The authenticated caller (injected).
        svc: The function service (injected).
        container: The pod container to read (default the user-container).
        sinceSeconds: Only return logs newer than this many seconds.
        limitBytes: Cap the bytes read per pod.

    Returns:
        The function's per-pod logs from the local site.
    """
    return await svc.logs(
        name, group, user, container=container, since_seconds=sinceSeconds, limit_bytes=limitBytes
    )


@router.delete("/{name}", status_code=204)
async def delete_function(group: Group, name: Name, user: CurrentUser, svc: FunctionDep) -> None:
    """Delete a function and its derived resources (204).

    Args:
        group: The owning group (from the request path).
        name: The workload name.
        user: The authenticated caller (injected).
        svc: The function service (injected).
    """
    await svc.delete(name, group, user)
