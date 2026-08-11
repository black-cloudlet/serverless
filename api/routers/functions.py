"""Function endpoints."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Query, Response
from fastapi.responses import StreamingResponse

from api.auth.deps import CurrentUser, StreamUser
from api.dependencies import FunctionDep
from api.models.common import (
    Group,
    Name,
    PodLogSnapshot,
    PodName,
    PodRoster,
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
    """Get one function, including the status rollup and per-site status.

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

    The lightweight endpoint to poll - the same ``status`` rollup as the full GET
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


@router.get("/{name}/pods", responses=sse.switchable(PodRoster, "`pods` and `error`"))
async def stream_function_pods(
    group: Group,
    name: Name,
    user: StreamUser,
    svc: FunctionDep,
    follow: bool = True,
    interval: Annotated[float | None, Query(gt=0)] = None,
) -> Response:
    """The function's pods on the current site - streamed, or read once.

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
        svc: The function service (injected).
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
async def stream_function_pod_logs(
    group: Group,
    name: Name,
    pod: PodName,
    user: StreamUser,
    svc: FunctionDep,
    follow: bool = True,
    container: str = "user-container",
    sinceSeconds: Annotated[int | None, Query(gt=0)] = None,
    limitBytes: Annotated[int | None, Query(gt=0)] = None,
    tailLines: Annotated[int | None, Query(gt=0)] = None,
) -> Response:
    """Follow one of the function's pods' logs, or read what it holds right now.

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
        svc: The function service (injected).
        follow: Stream the log (default), or return what the node holds now.
        container: The pod container to read (default the user-container).
        sinceSeconds: Start the log this many seconds back.
        limitBytes: Cap the bytes read; ``follow=false`` only, clamped to the
            deployment's snapshot ceiling.
        tailLines: Start at the newest this-many lines instead, however old
            they are - the right opening for a pod that has been quiet longer
            than any time window. Clamped to the deployment's snapshot bound.

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
            tail_lines=tailLines,
        )
    return sse.stream(
        await svc.stream_pod_logs(
            name,
            group,
            user,
            pod=pod,
            container=container,
            since_seconds=sinceSeconds,
            tail_lines=tailLines,
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
