"""Streaming a workload's live state: the same rollup, pushed instead of polled.

There is no watch to hang this off. Replica counts come from a Revision and
usage from ``metrics.k8s.io``, and neither is fresher than the metrics-server
scrape however it is read (docs/ARCHITECTURE.md - Observability), so the read
itself is still periodic. What changes is where the period lives: one connection
re-read on the server's own interval, on the stream pool, instead of every client
re-establishing a request several times a second against the shared one.

That also makes the failure mode better. A poll that starts 404ing is a client's
problem to interpret; here the workload going away ends the stream with an
``error`` event carrying the same code the envelope would have.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

from cloudlet_apis.errors import APIError
from cloudlet_apis.logging import get_logger

from api.core.config import StreamConfig
from api.models.common import StreamEnd, StreamError, WorkloadStatsResponse
from api.services.streams.sse import StreamEvent, heartbeat

logger = get_logger(__name__)

# What a read must look like: no arguments, and everything already bound.
StatsReader = Callable[[], Awaitable[WorkloadStatsResponse]]


async def follow(
    *,
    config: StreamConfig,
    first: WorkloadStatsResponse,
    read: StatsReader,
    interval: float,
) -> AsyncIterator[StreamEvent]:
    """Emit the workload's live state on an interval until the stream ends.

    Args:
        config: The stream bounds (lifetime and heartbeat cadence).
        first: The reading the caller already took to authorize the request,
            emitted immediately so a client has state before the first interval
            elapses rather than staring at an empty panel for it.
        read: Takes the next reading.
        interval: Seconds between readings.

    Yields:
        A ``stats`` event per reading, heartbeats in between when the interval is
        long, and a final ``error`` event if a reading fails.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + config.max_seconds
    yield StreamEvent("stats", first)

    while True:
        # Slept in heartbeat-sized pieces rather than one interval: at the top of
        # the configured range a reading is a minute apart, and a connection that
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
            # Said out loud, exactly as the log stream does: without an `end` a
            # client cannot tell the scheduled rollover from a dropped connection.
            yield StreamEvent(
                "end", StreamEnd(reason="the stream reached its time limit; reconnect")
            )
            return

        try:
            reading = await read()
        except APIError as exc:
            # The workload was deleted, or every site stopped answering. The
            # response has long since started, so the status code is spent and
            # this is the only way left to say so.
            yield StreamEvent("error", StreamError(code=exc.code, message=exc.message))
            return
        except Exception:  # noqa: BLE001 - mirrors the catch-all the envelope has
            logger.exception("stats stream read failed")
            yield StreamEvent(
                "error", StreamError(code=APIError.code, message="Internal server error.")
            )
            return
        yield StreamEvent("stats", reading)
