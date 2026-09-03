"""Streaming which pods a workload currently has, on the local region.

A pod name is the path segment ``/logs/pods/{pod}`` takes, so a client reads this
roster first and then opens a log stream for one of its entries.

It pushes on an interval rather than answering once because the roster expires:
Knative replaces a workload's pods on every revision and removes them all on
scale-to-zero (docs/ARCHITECTURE.md - Streaming).

Local region only, matching the log streams it feeds: a pod name is only useful
where its log can be read.

Two reads per tick. The Pod list is authoritative membership. The PodMetrics list
is usage, joined on by name and best-effort - metrics-server may not yet have
scraped a pod that started a second ago, and a missing measurement never hides
the pod.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime

from cloudlet_apis.errors import APIError
from cloudlet_apis.logging import get_logger

from api.core.config import StreamConfig
from api.models.common import PodInfo, PodRoster, StreamEnd, StreamError
from api.services.state import metrics as metrics_svc
from api.services.state.ksvc_state import ISRAEL_TZ
from api.services.streams.capacity import StreamCapacity
from api.services.streams.sse import StreamEvent, heartbeat
from common.cluster import NamespacedCluster, ResourceKind

logger = get_logger(__name__)

REVISION_LABEL = "serving.knative.dev/revision"
SERVICE_LABEL = "serving.knative.dev/service"

# Knative injects this into every pod; its usage is the platform's, not the
# user's, and it is excluded here for the same reason /stats excludes it.
_SIDECAR = "queue-proxy"


def _selector(workload: str) -> str:
    """The label selector matching one workload's pods."""
    return f"{SERVICE_LABEL}={workload}"


def _started(pod: dict) -> datetime | None:
    """The pod's start time in Israel local time, or None if unset/unparseable."""
    raw = (pod.get("status", {}) or {}).get("startTime")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(ISRAEL_TZ)
    except ValueError:
        return None


def _ready(pod: dict) -> bool:
    """Whether the pod's Ready condition is true.

    Reported next to ``phase`` rather than folded into it: a pod is ``Running``
    from the moment its containers start, which is before it serves traffic.
    """
    conditions = (pod.get("status", {}) or {}).get("conditions", []) or []
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


def _restarts(pod: dict) -> int:
    """Restarts summed over the pod's containers, sidecar included.

    The sidecar is counted here though its *usage* is not: a queue-proxy that
    keeps restarting is a pod that keeps dropping traffic.
    """
    statuses = (pod.get("status", {}) or {}).get("containerStatuses", []) or []
    return sum(int(s.get("restartCount") or 0) for s in statuses)


def _usage_by_pod(items: list[dict]) -> dict[str, metrics_svc.Usage]:
    """Index PodMetrics by pod name, summing each pod's user containers.

    Args:
        items: PodMetrics objects for the workload's pods.

    Returns:
        ``{pod_name: usage}``, omitting any pod that reported nothing.
    """
    measured: dict[str, metrics_svc.Usage] = {}
    for item in items:
        name = (item.get("metadata", {}) or {}).get("name")
        if not name:
            continue
        # Reuses the /stats summation, so a pod's figure here and its contribution
        # to the rollup there cannot be computed two different ways.
        total = metrics_svc.total_usage([item])
        if total is not None:
            measured[name] = total
    return measured


def read_roster(cluster: NamespacedCluster, workload: str) -> list[PodInfo]:
    """The workload's pods on this region, with usage joined on (blocking).

    Args:
        cluster: The local region.
        workload: The workload's name (the KSVC label value).

    Returns:
        The pods, ordered by name. Empty when scaled to zero.
    """
    pods = cluster.get(ResourceKind.POD, label_selector=_selector(workload))
    # Best-effort, and read after the authoritative pod list: an unreadable
    # metrics API leaves usage null instead of emptying the roster.
    try:
        measured = cluster.get(ResourceKind.POD_METRICS, label_selector=_selector(workload))
        usage = _usage_by_pod(measured)
    except Exception:  # noqa: BLE001 - usage is best-effort, never fatal
        usage = {}

    roster: list[PodInfo] = []
    for pod in pods:
        meta = pod.get("metadata", {}) or {}
        name = meta.get("name")
        if not name:
            continue
        pod_usage = usage.get(name)
        roster.append(
            PodInfo(
                pod=name,
                revision=(meta.get("labels", {}) or {}).get(REVISION_LABEL),
                phase=(pod.get("status", {}) or {}).get("phase") or "Unknown",
                ready=_ready(pod),
                restarts=_restarts(pod),
                startedAt=_started(pod),
                usage=pod_usage.quantities() if pod_usage else None,
            )
        )
    return sorted(roster, key=lambda p: p.pod)


async def follow(
    *,
    cluster: NamespacedCluster,
    capacity: StreamCapacity,
    config: StreamConfig,
    first: PodRoster,
    workload: str,
    interval: float,
) -> AsyncIterator[StreamEvent]:
    """Push the pod roster on an interval until the client leaves or time is up.

    Args:
        cluster: The local region.
        capacity: The stream pool the listings run on.
        config: The stream bounds.
        first: The roster the caller already read to authorize the request,
            emitted immediately so a client can open a log stream without waiting
            out an interval first.
        workload: The workload's name (the KSVC label value).
        interval: Seconds between listings.

    Yields:
        A ``pods`` event per reading, heartbeats when an interval is long, and a
        final ``error`` if a reading fails.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + config.max_seconds
    yield StreamEvent("pods", first)

    while True:
        # Slept in heartbeat-sized pieces, not in one interval: at the top of the
        # configured range a listing is a minute apart, and a connection that
        # sends nothing for that long is reaped by an idle timeout in the path.
        due = min(loop.time() + interval, deadline)
        while True:
            now = loop.time()
            if now >= due:
                break
            await asyncio.sleep(min(config.heartbeat_seconds, due - now))
            if loop.time() < due:
                yield heartbeat()
        if loop.time() >= deadline:
            # An explicit `end`, as the log stream sends: it tells the client the
            # scheduled rollover apart from a dropped connection.
            yield StreamEvent(
                "end", StreamEnd(reason="the stream reached its time limit; reconnect")
            )
            return

        try:
            pods = await capacity.run(read_roster, cluster, workload)
        except APIError as exc:
            # The workload was deleted, or the region stopped answering. The
            # response has long since started, so the status code is spent.
            yield StreamEvent("error", StreamError(code=exc.code, message=exc.message))
            return
        except Exception:  # noqa: BLE001 - mirrors the catch-all the envelope has
            logger.exception("pod roster read failed for '%s'", workload)
            yield StreamEvent(
                "error", StreamError(code=APIError.code, message="Internal server error.")
            )
            return
        yield StreamEvent("pods", first.model_copy(update={"pods": pods}))
