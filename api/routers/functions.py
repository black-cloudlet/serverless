"""Function endpoints."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Body, Query, Response
from fastapi.responses import JSONResponse, StreamingResponse

from api.auth.deps import BuildCaller, CurrentUser, StreamUser, WebhookCaller
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
from api.models.function import (
    FunctionCreate,
    FunctionResponse,
    FunctionUpdate,
    WebhookView,
)
from api.models.webhook import GitLabPushEvent, WebhookOutcome
from api.routers import streaming

router = APIRouter(prefix="/groups/{group}/functions", tags=["functions"])


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


@router.post(
    "/{name}/build",
    response_model=FunctionResponse,
    status_code=202,
    responses={200: {"model": WebhookOutcome, "description": "Delivery ignored (git webhook)"}},
)
async def build_function(
    group: Group,
    name: Name,
    caller: BuildCaller,
    svc: FunctionDep,
    background: BackgroundTasks,
    event: Annotated[GitLabPushEvent | None, Body()] = None,
) -> Response | FunctionResponse:
    """Build a function again (202), no body - or take a git push that says so.

    **With a bearer token**, the build inputs are the stored ones - repository,
    revision, path, runtime, version and the saved git token - so this rebuilds
    the same definition against today's base image and dependencies, and returns
    the function to its revision's head by clearing any commit a push pinned.
    The spec does not change and the running revision keeps serving.

    **With `X-Gitlab-Token`**, this is the function's git webhook. The push
    builds only if it updated the branch the function's `revision` names, in the
    repository it builds from; anything else is `200` with `accepted: false`,
    since a provider disables a hook that keeps failing. A push changes the
    commit built and nothing else - not the revision, the tag, or the spec
    (docs/FUNCTIONS.md - Git webhook).

    Args:
        group: The owning group (from the request path).
        name: The workload name.
        caller: The authenticated user, or the git provider (injected).
        svc: The function service (injected).
        background: FastAPI background tasks (injected).
        event: The push payload, when a provider sent one.

    Returns:
        A Pending response with a ``statusUrl`` to poll for the build outcome,
        or the outcome of a delivery that started no build.
    """
    if isinstance(caller, WebhookCaller):
        outcome = await svc.accept_webhook(
            group, name, caller.token, event, caller.event, background
        )
        if isinstance(outcome, WebhookOutcome):
            return JSONResponse(status_code=200, content=outcome.model_dump())
        return outcome
    return await svc.accept_build(group, name, caller, background)


@router.post("/{name}/webhook/rotate", response_model=WebhookView)
async def rotate_function_webhook(
    group: Group, name: Name, user: CurrentUser, svc: FunctionDep
) -> WebhookView:
    """Replace this function's webhook token and return the new one (200).

    Every region is written before this answers, so the old token stops working
    at once and a leaked one does not outlive the request replacing it.
    Reconfigure the hook with what comes back.

    There is no endpoint to *disable* a hook: a token nothing calls starts no
    build, so that is done in the git provider.

    Args:
        group: The owning group (from the request path).
        name: The workload name.
        user: The authenticated caller (injected).
        svc: The function service (injected).

    Returns:
        The new webhook configuration.
    """
    return await svc.rotate_webhook(group, name, user)


@router.get("", response_model=list[WorkloadSummary])
async def list_functions(
    group: Group,
    user: CurrentUser,
    svc: FunctionDep,
    sort: Literal["name", "createdAt"] = "name",
) -> list[WorkloadSummary]:
    """List general info for every function the group owns (merged across regions).

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
    """Get one function, including the status rollup and per-region status.

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
    """Get the function's live state: status, replicas and usage, per region.

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


@router.get("/{name}/pods", responses=streaming.switchable(PodRoster, "`pods` and `error`"))
async def stream_function_pods(
    group: Group,
    name: Name,
    user: StreamUser,
    svc: FunctionDep,
    follow: bool = True,
    interval: Annotated[float | None, Query(gt=0)] = None,
) -> Response:
    """The function's pods on the current region - streamed, or read once.

    Streams by default: Knative replaces a workload's pods on every revision and
    removes them all on scale-to-zero, so the roster changes underneath a client.
    ``follow=false`` returns a single JSON roster instead, for a caller that
    cannot hold a connection open.

    Local region only, like the log endpoint it feeds. This is where the
    ``{pod}`` for ``/logs/pods/{pod}`` comes from; nothing else in the API
    returns one.

    Events (when following): ``pods`` (the full roster, the first sent
    immediately) and ``error``. Lines beginning with ``:`` are heartbeats. An
    empty roster is normal - the workload is deployed here and scaled to zero.

    Browsers authenticate a stream with ``?ticket=`` from
    ``POST /api/serverless/v1/stream-tickets``; everything else sends the usual
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
    return streaming.stream(await svc.stream_pods(name, group, user, interval=interval))


@router.get(
    "/{name}/logs/pods/{pod}",
    responses=streaming.switchable(PodLogSnapshot, "`open`, `log`, `warning`, `end` and `error`"),
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

    Current region only, either way: Kubernetes keeps no log buffer beyond the node
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
        tailLines: Start at the newest this-many lines instead, however old they
            are; unlike ``sinceSeconds`` it is bounded by a line count, not a
            time window. Clamped to the deployment's snapshot bound.

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
    return streaming.stream(
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


@router.get("/{name}/stats/stream", responses=streaming.RESPONSES, response_class=StreamingResponse)
async def stream_function_stats(
    group: Group,
    name: Name,
    user: StreamUser,
    svc: FunctionDep,
    interval: Annotated[float | None, Query(gt=0)] = None,
) -> StreamingResponse:
    """Follow the function's live state (Server-Sent Events).

    The streaming form of ``/stats``, pushing exactly the same body every
    ``interval`` seconds instead of on request.

    Events: ``stats`` (a :class:`WorkloadStatsResponse`, the first sent
    immediately) and ``error`` (the workload is gone, or no region could answer).
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
    return streaming.stream(await svc.stream_stats(name, group, user, interval=interval))


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
