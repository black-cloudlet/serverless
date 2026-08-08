"""Turning a service's event stream into an SSE response.

One helper, shared by both offerings' routers, so the two cannot drift in the
headers they set or in what they do when a stream fails after it has begun.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from cloudlet_apis.errors import APIError
from cloudlet_apis.logging import get_logger
from fastapi.responses import StreamingResponse

from api.models.common import StreamError
from api.services.streams import sse

logger = get_logger(__name__)

# What the OpenAPI document says about a streaming route. FastAPI infers a
# response schema from the return annotation, and a StreamingResponse has none
# to infer - without this the operation documents itself as returning nothing.
RESPONSES: dict = {
    200: {
        "description": (
            "An event stream (text/event-stream). Named events: see the endpoint "
            "description. A `:` line is a heartbeat and carries no event."
        ),
        "content": {sse.MEDIA_TYPE: {"schema": {"type": "string"}}},
    }
}


def stream(events: AsyncIterator[sse.StreamEvent]) -> StreamingResponse:
    """Render a service's events as an SSE response.

    Args:
        events: The event stream, already authorized and opened by the service -
            so anything that should be a status code has been raised before this
            is called, and an error envelope was still possible.

    Returns:
        The streaming response.
    """

    async def body() -> AsyncIterator[str]:
        yield sse.preamble()
        try:
            async for event in events:
                yield sse.render(event)
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

    return StreamingResponse(body(), media_type=sse.MEDIA_TYPE, headers=dict(sse.HEADERS))
