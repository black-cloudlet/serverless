"""Streaming which pods a workload currently has, on the local site.

This is the endpoint that makes per-pod log streaming usable: a pod name is the
path segment ``/logs/pods/{pod}`` takes, and until this existed the only way to
learn one was to read every pod's logs.

It is a stream rather than a lookup because the answer expires. Knative replaces
a workload's pods on every revision and removes them all on scale-to-zero, so a
roster fetched once is a roster that quietly stops being true - a client would
have to poll it at exactly the cadence this pushes at anyway.

Local site only, matching the log streams it feeds: a pod name is only useful
where its log can be read.

Two reads per tick, and they are different kinds of thing. The Pod list is
authoritative membership. The PodMetrics list is usage, joined on by name and
best-effort - metrics-server has not scraped a pod that started a second ago, and
that is precisely the pod a client is most likely to want to follow, so a missing
measurement must never hide the pod.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime

from cloudlet_apis.errors import APIError
from cloudlet_apis.logging import get_logger

from api.core.config import StreamConfig
from api.models.common import PodInfo, PodRoster, StreamError
from api.services.state import metrics as metrics_svc
from api.services.state.ksvc_state import ISRAEL_TZ
from api.services.streams.capacity import StreamCapacity
from api.services.streams.sse import StreamEvent, heartbeat
from common.cluster import Cluster, ResourceKind

logger = get_logger(__name__)

REVISION_LABEL = "serving.knative.dev/revision"
SERVICE_LABEL = "serving.knative.dev/service"

# Knative injects this into every pod; its usage is the platform's, not the
# user's, and it is excluded here for the same reason /stats excludes it.
_SIDECAR = "queue-proxy"


def _selector(oname: str) -> str:
    """The label selector matching one workload's pods."""
    return f"{SERVICE_LABEL}={oname}"


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
    from the moment its containers start, which is before it serves traffic, and
    conflating the two is how a UI shows a workload as up while requests fail.
    """
    conditions = (pod.get("status", {}) or {}).get("conditions", []) or []
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


def _restarts(pod: dict) -> int:
    """Restarts summed over the pod's containers, sidecar included.

    The sidecar is counted here though its *usage* is not: a queue-proxy that
    keeps restarting is a pod that keeps dropping traffic, which is exactly what
    someone reading this is trying to find out.
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


def read_roster(cluster: Cluster, oname: str) -> list[PodInfo]:
    """The workload's pods on this site, with usage joined on (blocking).

    Args:
        cluster: The local site.
        oname: The object name (``{name}-{group}``).

    Returns:
        The pods, ordered by name. Empty when scaled to zero.
    """
    pods = cluster.get(ResourceKind.POD, label_selector=_selector(oname))
    # Best-effort, and deliberately after the authoritative list: usage is a
    # decoration, and an unreadable metrics API must not empty the roster.
    try:
        measured = cluster.get(ResourceKind.POD_METRICS, label_selector=_selector(oname))
        usage = _usage_by_pod(measured)
    except Exception:  # noqa: BLE001 - usage is best-effort, never fatal
        usage = {}

    roster: list[PodInfo] = []
    for pod in pods:
        meta = pod.get("metadata", {}) or {}
        name = meta.get("name")
        if not name:
            continue
        measured = usage.get(name)
        roster.append(
            PodInfo(
                pod=name,
                revision=(meta.get("labels", {}) or {}).get(REVISION_LABEL),
                phase=(pod.get("status", {}) or {}).get("phase") or "Unknown",
                ready=_ready(pod),
                restarts=_restarts(pod),
                startedAt=_started(pod),
                usage=measured.quantities() if measured else None,
            )
        )
    return sorted(roster, key=lambda p: p.pod)


async def follow(
    *,
    cluster: Cluster,
    capacity: StreamCapacity,
    config: StreamConfig,
    first: PodRoster,
    oname: str,
    interval: float,
) -> AsyncIterator[StreamEvent]:
    """Push the pod roster on an interval until the client leaves or time is up.

    Args:
        cluster: The local site.
        capacity: The stream pool the listings run on.
        config: The stream bounds.
        first: The roster the caller already read to authorize the request,
            emitted immediately so a client can open a log stream without waiting
            out an interval first.
        oname: The object name (``{name}-{group}``).
        interval: Seconds between listings.

    Yields:
        A ``pods`` event per reading, heartbeats when an interval is long, and a
        final ``error`` if a reading fails.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + config.max_seconds
    yield StreamEvent("pods", first)

    while True:
        # Slept in heartbeat-sized pieces rather than one interval: at the top of
        # the configured range a listing is a minute apart, and a connection that
        # sends nothing for a minute is one an idle-timeout somewhere in the path
        # will close out from under both ends.
        due = min(loop.time() + interval, deadline)
        while True:
            now = loop.time()
            if now >= due:
                break
            await asyncio.sleep(min(config.heartbeat_seconds, due - now))
            if loop.time() < due:
                yield heartbeat()
        if loop.time() >= deadline:
            return  # the client reconnects; SSE does that on its own

        try:
            pods = await capacity.run(read_roster, cluster, oname)
        except APIError as exc:
            # The workload was deleted, or the site stopped answering. The
            # response has long since started, so the status code is spent.
            yield StreamEvent("error", StreamError(code=exc.code, message=exc.message))
            return
        except Exception:  # noqa: BLE001 - mirrors the catch-all the envelope has
            logger.exception("pod roster read failed for '%s'", oname)
            yield StreamEvent(
                "error", StreamError(code=APIError.code, message="Internal server error.")
            )
            return
        yield StreamEvent("pods", first.model_copy(update={"pods": pods}))
