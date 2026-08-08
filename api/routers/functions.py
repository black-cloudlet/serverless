"""Function endpoints."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Query
from fastapi.responses import StreamingResponse

from api.auth.deps import CurrentUser, StreamUser
from api.dependencies import FunctionDep
from api.models.common import (
    Group,
    LogsResponse,
    Name,
    WorkloadStatsResponse,
    WorkloadSummary,
)
from api.models.function import FunctionCreate, FunctionResponse, FunctionUpdate
from api.routers import sse

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


@router.post("/{name}/build", response_model=FunctionResponse, status_code=202)
async def build_function(
    group: Group,
    name: Name,
    user: CurrentUser,
    svc: FunctionDep,
    background: BackgroundTasks,
) -> FunctionResponse:
    """Rebuild a function from its current source (202), no body.

    The build inputs are the ones already stored - repository, branch, path,
    runtime, version and the saved git token - so this rebuilds the same
    definition against today's base image and dependencies. Nothing about the
    workload's spec changes and the running revision keeps serving.

    Args:
        group: The owning group (from the request path).
        name: The workload name.
        user: The authenticated caller (injected).
        svc: The function service (injected).
        background: FastAPI background tasks (injected).

    Returns:
        A Pending response with a ``statusUrl`` to poll for the build outcome.
    """
    return await svc.accept_build(group, name, user, background)


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


@router.get("/{name}/stats", response_model=WorkloadStatsResponse)
async def get_function_stats(
    group: Group, name: Name, user: CurrentUser, svc: FunctionDep
) -> WorkloadStatsResponse:
    """Get the function's live state: status, replicas and usage, per site.

    The lightweight endpoint to poll - the same ``overallStatus`` as the full GET
    and the live numbers behind it, and none of the desired-state config.

    Args:
        group: The owning group (from the request path).
        name: The workload name.
        user: The authenticated caller (injected).
        svc: The function service (injected).

    Returns:
        The function's live stats view.
    """
    return await svc.stats(name, group, user)


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


@router.get("/{name}/logs/stream", responses=sse.RESPONSES, response_class=StreamingResponse)
async def stream_function_logs(
    group: Group,
    name: Name,
    user: StreamUser,
    svc: FunctionDep,
    container: str = "user-container",
    sinceSeconds: Annotated[int | None, Query(gt=0)] = None,
    interval: Annotated[float | None, Query(gt=0)] = None,
) -> StreamingResponse:
    """Follow the function's pod logs from the current site (Server-Sent Events).

    The streaming form of ``/logs``, and local-site only for the same reason -
    logs live on the node that wrote them. Unlike the snapshot, this keeps up
    with the workload: pods are re-listed every ``interval`` seconds, so a
    scale-up or a new revision starts being followed without reconnecting.

    Events: ``open`` (what is being followed), ``log`` (one line), ``pods`` (the
    followed set changed), ``warning`` (degraded but still running - a pod cap
    hit, or lines dropped because the client read too slowly), ``error`` (ending,
    and why). Lines beginning with ``:`` are heartbeats.

    Browsers authenticate with ``?ticket=`` from ``POST /api/v1/stream-tickets``;
    everything else sends the usual ``Authorization`` header.

    Args:
        group: The owning group (from the request path).
        name: The workload name.
        user: The authenticated caller, by header or ticket (injected).
        svc: The function service (injected).
        container: The pod container to read (default the user-container).
        sinceSeconds: Start each pod's log this many seconds back.
        interval: Seconds between pod re-listings; omit for the default.

    Returns:
        The event stream.
    """
    return sse.stream(
        await svc.stream_logs(
            name,
            group,
            user,
            container=container,
            since_seconds=sinceSeconds,
            interval=interval,
        )
    )


@router.get("/{name}/stats/stream", responses=sse.RESPONSES, response_class=StreamingResponse)
async def stream_function_stats(
    group: Group,
    name: Name,
    user: StreamUser,
    svc: FunctionDep,
    interval: Annotated[float | None, Query(gt=0)] = None,
) -> StreamingResponse:
    """Follow the function's live state (Server-Sent Events).

    The streaming form of ``/stats``, reporting exactly the same body on an
    interval instead of on request. One connection replaces a client's poll
    loop, so the fan-out happens once per interval however many clients are
    watching.

    Events: ``stats`` (a :class:`WorkloadStatsResponse`, the first sent
    immediately) and ``error`` (the workload is gone, or no site could answer).
    Lines beginning with ``:`` are heartbeats.

    Args:
        group: The owning group (from the request path).
        name: The workload name.
        user: The authenticated caller, by header or ticket (injected).
        svc: The function service (injected).
        interval: Seconds between readings; omit for the default.

    Returns:
        The event stream.
    """
    return sse.stream(await svc.stream_stats(name, group, user, interval=interval))


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
