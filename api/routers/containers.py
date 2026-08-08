"""Container endpoints."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Query
from fastapi.responses import StreamingResponse

from api.auth.deps import CurrentUser, StreamUser
from api.dependencies import ContainerDep
from api.models.common import (
    Group,
    LogsResponse,
    Name,
    WorkloadStatsResponse,
    WorkloadSummary,
)
from api.models.container import ContainerCreate, ContainerResponse, ContainerUpdate
from api.routers import sse

router = APIRouter(prefix="/api/v1/groups/{group}/containers", tags=["containers"])


@router.post("", response_model=ContainerResponse, status_code=202)
async def create_container(
    group: Group,
    spec: ContainerCreate,
    user: CurrentUser,
    svc: ContainerDep,
    background: BackgroundTasks,
) -> ContainerResponse:
    """Create a container (202): validate synchronously, deploy in the background.

    Args:
        group: The owning group (from the request path).
        spec: The container create request.
        user: The authenticated caller (injected).
        svc: The container service (injected).
        background: FastAPI background tasks (injected).

    Returns:
        A Pending response with a ``statusUrl`` to poll for the deploy outcome.
    """
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
    """Update a container (202): full replace of the mutable spec, applied async.

    Args:
        group: The owning group (from the request path).
        name: The workload name.
        spec: The container update request.
        user: The authenticated caller (injected).
        svc: The container service (injected).
        background: FastAPI background tasks (injected).

    Returns:
        A Pending response with a ``statusUrl`` to poll.
    """
    return await svc.accept_update(group, name, spec, user, background)


@router.post("/{name}/pull", response_model=ContainerResponse, status_code=202)
async def pull_container(
    group: Group,
    name: Name,
    user: CurrentUser,
    svc: ContainerDep,
    background: BackgroundTasks,
) -> ContainerResponse:
    """Pull the image tag again (202), no body.

    Knative resolves a tag to a digest once, at revision creation, so an image
    pushed over the same tag is never picked up. This cuts a new revision in
    every site, which resolves the tag again; nothing else changes.

    Args:
        group: The owning group (from the request path).
        name: The workload name.
        user: The authenticated caller (injected).
        svc: The container service (injected).
        background: FastAPI background tasks (injected).

    Returns:
        A Pending response with a ``statusUrl`` to poll.
    """
    return await svc.accept_pull(group, name, user, background)


@router.get("", response_model=list[WorkloadSummary])
async def list_containers(
    group: Group,
    user: CurrentUser,
    svc: ContainerDep,
    sort: Literal["name", "createdAt"] = "name",
) -> list[WorkloadSummary]:
    """List general info for every container the group owns (merged across sites).

    Args:
        group: The owning group (from the request path).
        user: The authenticated caller (injected).
        svc: The container service (injected).
        sort: Sort key, "name" or "createdAt".

    Returns:
        The per-workload summaries.
    """
    return await svc.list(group, user, sort)


@router.get("/{name}", response_model=ContainerResponse)
async def get_container(
    group: Group, name: Name, user: CurrentUser, svc: ContainerDep
) -> ContainerResponse:
    """Get one container, including overallStatus and per-site status.

    This is the poll target advertised as ``statusUrl`` on the 202 accept
    response.

    Args:
        group: The owning group (from the request path).
        name: The workload name.
        user: The authenticated caller (injected).
        svc: The container service (injected).

    Returns:
        The full single-container response.
    """
    return await svc.get(name, group, user)


@router.get("/{name}/stats", response_model=WorkloadStatsResponse)
async def get_container_stats(
    group: Group, name: Name, user: CurrentUser, svc: ContainerDep
) -> WorkloadStatsResponse:
    """Get the container's live state: status, replicas and usage, per site.

    The lightweight endpoint to poll - the same ``overallStatus`` as the full GET
    and the live numbers behind it, and none of the desired-state config.

    Args:
        group: The owning group (from the request path).
        name: The workload name.
        user: The authenticated caller (injected).
        svc: The container service (injected).

    Returns:
        The container's live stats view.
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

    Args:
        group: The owning group (from the request path).
        name: The workload name.
        user: The authenticated caller (injected).
        svc: The container service (injected).
        container: The pod container to read (default the user-container).
        sinceSeconds: Only return logs newer than this many seconds.
        limitBytes: Cap the bytes read per pod.

    Returns:
        The container's per-pod logs from the local site.
    """
    return await svc.logs(
        name, group, user, container=container, since_seconds=sinceSeconds, limit_bytes=limitBytes
    )


@router.get("/{name}/logs/stream", responses=sse.RESPONSES, response_class=StreamingResponse)
async def stream_container_logs(
    group: Group,
    name: Name,
    user: StreamUser,
    svc: ContainerDep,
    container: str = "user-container",
    sinceSeconds: Annotated[int | None, Query(gt=0)] = None,
    interval: Annotated[float | None, Query(gt=0)] = None,
) -> StreamingResponse:
    """Follow the container's pod logs from the current site (Server-Sent Events).

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
        svc: The container service (injected).
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
async def stream_container_stats(
    group: Group,
    name: Name,
    user: StreamUser,
    svc: ContainerDep,
    interval: Annotated[float | None, Query(gt=0)] = None,
) -> StreamingResponse:
    """Follow the container's live state (Server-Sent Events).

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
        svc: The container service (injected).
        interval: Seconds between readings; omit for the default.

    Returns:
        The event stream.
    """
    return sse.stream(await svc.stream_stats(name, group, user, interval=interval))


@router.delete("/{name}", status_code=204)
async def delete_container(group: Group, name: Name, user: CurrentUser, svc: ContainerDep) -> None:
    """Delete a container and its derived resources (204).

    Args:
        group: The owning group (from the request path).
        name: The workload name.
        user: The authenticated caller (injected).
        svc: The container service (injected).
    """
    await svc.delete(name, group, user)
