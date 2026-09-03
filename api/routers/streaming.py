"""Turning a service's event stream into an SSE response.

Shared by both offerings' routers: it sets the SSE headers, renders each event,
turns a failure after the first byte into an ``error`` event, and closes the
service's generator on teardown (docs/STREAMING.md - The streams).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from cloudlet_apis.errors import APIError
from cloudlet_apis.logging import get_logger
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from api.models.common import StreamError
from api.services.streams import sse

logger = get_logger(__name__)

# What the OpenAPI document says about a streaming route. FastAPI infers a
# response schema from the return annotation, and a StreamingResponse carries
# none, so the 200 is declared here instead.
RESPONSES: dict = {
    200: {
        "description": (
            "An event stream (text/event-stream). Named events: see the endpoint "
            "description. A `:` line is a heartbeat and carries no event."
        ),
        "content": {sse.MEDIA_TYPE: {"schema": {"type": "string"}}},
    }
}


def switchable(snapshot: type[BaseModel], events: str) -> dict:
    """The OpenAPI 200 for a route that streams by default and can be asked not to.

    One operation with two media types, chosen by ``follow``: ``text/event-stream``
    for a follow and ``application/json`` for the snapshot. Both are declared on
    the 200 so a generated client knows about either form.

    Args:
        snapshot: The model returned when ``follow=false``.
        events: The named events the stream emits, for the description.

    Returns:
        The ``responses`` mapping for the route.
    """
    return {
        200: {
            "description": (
                f"`follow=true` (the default): an event stream (text/event-stream) "
                f"emitting {events}; a `:` line is a heartbeat and carries no event. "
                f"`follow=false`: a single JSON {snapshot.__name__}, read once."
            ),
            "content": {
                sse.MEDIA_TYPE: {"schema": {"type": "string"}},
                "application/json": {"schema": snapshot.model_json_schema()},
            },
        }
    }


def stream(events: AsyncIterator[sse.StreamEvent | str]) -> StreamingResponse:
    """Render a service's events as an SSE response.

    Args:
        events: The event stream, already authorized and opened by the service:
            anything that should be a status code has been raised before this is
            called, while an error envelope was still possible. A str item is a
            frame the service already rendered off the event loop (the log
            streams' line path - see api.services.streams.logs) and passes
            through untouched.

    Returns:
        The streaming response.
    """

    async def body() -> AsyncIterator[str]:
        try:
            yield sse.preamble()
            async for event in events:
                yield event if isinstance(event, str) else sse.render(event)
        except APIError as exc:
            # The status line went out with the first byte, so this is the only
            # place left to say what went wrong.
            yield sse.render(
                sse.StreamEvent("error", StreamError(code=exc.code, message=exc.message))
            )
        except Exception:  # noqa: BLE001 - mirrors the envelope's catch-all
            logger.exception("stream failed")
            yield sse.render(
                sse.StreamEvent(
                    "error", StreamError(code=APIError.code, message="Internal server error.")
                )
            )
        finally:
            # Deterministic teardown. A client disconnect cancels the response
            # task and lands here as GeneratorExit; the explicit aclose runs the
            # stream's own teardown - the admission slot, the follower thread,
            # the open log socket - instead of leaving it to the GC.
            aclose = getattr(events, "aclose", None)
            if aclose is not None:
                await aclose()

    return StreamingResponse(body(), media_type=sse.MEDIA_TYPE, headers=dict(sse.HEADERS))
