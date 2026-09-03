"""Streaming a workload's live state: the same rollup, pushed instead of polled.

There is no watch to hang this off. Replica counts come from a Revision and
usage from ``metrics.k8s.io``, and neither is fresher than the metrics-server
scrape however it is read, so the read
itself stays periodic: one connection, re-read on the server's own interval, on
the stream pool.

A workload that goes away ends the stream with an ``error`` event carrying the
same code the envelope would have
(docs/STREAMING.md - Errors after the first byte).
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
            emitted immediately so the client has state before the first interval
            elapses.
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
        # Slept in heartbeat-sized pieces, not in one interval: at the top of the
        # configured range a reading is a minute apart, and a connection that
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
            reading = await read()
        except APIError as exc:
            # The workload was deleted, or every region stopped answering. The
            # response has long since started, so the status code is spent and
            # the failure is reported as an `error` event.
            yield StreamEvent("error", StreamError(code=exc.code, message=exc.message))
            return
        except Exception:  # noqa: BLE001 - mirrors the catch-all the envelope has
            logger.exception("stats stream read failed")
            yield StreamEvent(
                "error", StreamError(code=APIError.code, message="Internal server error.")
            )
            return
        yield StreamEvent("stats", reading)
