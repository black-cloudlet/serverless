"""Container endpoints."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Query, Response
from fastapi.responses import StreamingResponse

from api.auth.deps import CurrentUser, StreamUser
from api.dependencies import ContainerDep
from api.models.common import (
    Group,
    Name,
    PodLogSnapshot,
    PodName,
    PodRoster,
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
    """Get one container, including the status rollup and per-site status.

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

    The lightweight endpoint to poll - the same ``status`` rollup as the full GET
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


@router.get("/{name}/pods", responses=sse.switchable(PodRoster, "`pods` and `error`"))
async def stream_container_pods(
    group: Group,
    name: Name,
    user: StreamUser,
    svc: ContainerDep,
    follow: bool = True,
    interval: Annotated[float | None, Query(gt=0)] = None,
) -> Response:
    """The container's pods on the current site - streamed, or read once.

    Streams by default, because the answer expires: Knative replaces a workload's
    pods on every revision and removes them all on scale-to-zero, so a roster
    fetched once quietly stops being true. ``follow=false`` returns a single JSON
    roster instead, for a caller that cannot hold a connection open.

    Local site only, matching the log endpoint it feeds - a pod name is only
    useful where its log can be read. This is where the ``{pod}`` for
    ``/logs/pods/{pod}`` comes from; nothing else in the API returns one.

    Events (when following): ``pods`` (the full roster, the first sent
    immediately) and ``error``. Lines beginning with ``:`` are heartbeats. An
    empty roster is normal - the workload is deployed here and scaled to zero.

    Browsers authenticate a stream with ``?ticket=`` from
    ``POST /api/v1/stream-tickets``; everything else sends the usual
    ``Authorization`` header.

    Args:
        group: The owning group (from the request path).
        name: The workload name.
        user: The authenticated caller, by header or ticket (injected).
        svc: The container service (injected).
        follow: Stream the roster (default), or return it once.
        interval: Seconds between listings when following; omit for the default.

    Returns:
        The event stream, or the roster.
    """
    if not follow:
        return await svc.pods(name, group, user)
    return sse.stream(await svc.stream_pods(name, group, user, interval=interval))


@router.get(
    "/{name}/logs/pods/{pod}",
    responses=sse.switchable(PodLogSnapshot, "`open`, `log`, `warning`, `end` and `error`"),
)
async def stream_container_pod_logs(
    group: Group,
    name: Name,
    pod: PodName,
    user: StreamUser,
    svc: ContainerDep,
    follow: bool = True,
    container: str = "user-container",
    sinceSeconds: Annotated[int | None, Query(gt=0)] = None,
    limitBytes: Annotated[int | None, Query(gt=0)] = None,
) -> Response:
    """Follow one of the container's pods' logs, or read what it holds right now.

    Current site only, either way: Kubernetes keeps no log buffer beyond the node
    that wrote it. Get ``pod`` from ``GET .../{name}/pods``.

    Following is the default. ``follow=false`` returns a single JSON snapshot -
    the newest lines the node still holds, within the deployment's snapshot
    bounds (``stream.snapshotTailLines`` / ``snapshotMaxBytes``) - for a caller
    that cannot hold a connection open. ``limitBytes`` applies only to that
    form, and is clamped to the deployment's ceiling.

    A followed stream ends with an ``end`` event when the pod's log does - a
    scale-down or a new revision, which on Knative is routine and is not reported
    as an error. Pick the replacement pod off the ``pods`` endpoint.

    Events (when following): ``open``, ``log`` (one line), ``warning`` (lines
    dropped because the client read too slowly), ``end``, ``error``. Lines
    beginning with ``:`` are heartbeats. The snapshot returns those same lines in
    one body, so a client renders one shape either way.

    Args:
        group: The owning group (from the request path).
        name: The workload name.
        pod: The pod to read. A pod that is not this workload's is a 404.
        user: The authenticated caller, by header or ticket (injected).
        svc: The container service (injected).
        follow: Stream the log (default), or return what the node holds now.
        container: The pod container to read (default the user-container).
        sinceSeconds: Start the log this many seconds back.
        limitBytes: Cap the bytes read; ``follow=false`` only, clamped to the
            deployment's snapshot ceiling.

    Returns:
        The event stream, or the snapshot.
    """
    if not follow:
        return await svc.pod_logs(
            name,
            group,
            user,
            pod=pod,
            container=container,
            since_seconds=sinceSeconds,
            limit_bytes=limitBytes,
        )
    return sse.stream(
        await svc.stream_pod_logs(
            name, group, user, pod=pod, container=container, since_seconds=sinceSeconds
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
