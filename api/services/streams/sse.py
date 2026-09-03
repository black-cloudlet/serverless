"""Server-Sent Events framing: the wire format, and the event the services yield.

The framing lives apart from the streams themselves: the services yield typed
*events* and only the router turns them into bytes.

The format is the one the EventSource specification defines: ``field: value``
lines, a blank line ending each event, and a leading ``:`` marking a comment
that carries no event at all - which is what a heartbeat is.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

MEDIA_TYPE = "text/event-stream"

# Set on the response itself. `no-transform` and `X-Accel-Buffering` say the same
# thing to two different intermediaries: do not buffer this response.
HEADERS = {
    "Cache-Control": "no-cache, no-store, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

# How long a client waits before reconnecting, sent once at the top of a stream.
# A stream ends on its own every `max_seconds` (see StreamConfig), so reconnecting
# is the normal cadence and not an error path.
RETRY_MS = 2000


@dataclass(frozen=True)
class StreamEvent:
    """One thing to send a client: a named event, or a heartbeat.

    Attributes:
        name: The SSE event name a listener subscribes to (``log``, ``stats``,
            ``open``, ``pods``, ``warning``, ``error``).
        data: The payload model, or None for a heartbeat - which is rendered as
            a comment, so it keeps the connection alive without the client
            seeing an event at all.
    """

    name: str
    data: BaseModel | None = None

    @property
    def is_heartbeat(self) -> bool:
        """Whether this carries no payload and renders as a comment."""
        return self.data is None


def heartbeat() -> StreamEvent:
    """The empty event that only keeps the connection open."""
    return StreamEvent(name="heartbeat")


def render(event: StreamEvent) -> str:
    """Render one event as an SSE frame.

    Args:
        event: The event to send.

    Returns:
        The frame, terminated by the blank line that ends an SSE event.
    """
    if event.data is None:
        return f": {event.name}\n\n"
    body = event.data.model_dump_json()
    # One `data:` line per line of payload: an embedded newline would otherwise
    # truncate the event. Pydantic's JSON carries none, but the rule belongs to
    # the format rather than to the payload.
    lines = "".join(f"data: {line}\n" for line in body.split("\n"))
    return f"event: {event.name}\n{lines}\n"


def preamble() -> str:
    """The bytes sent before the first event.

    Carries the reconnection delay, and - being a comment - flushes the response
    headers through any intermediary that would otherwise hold them until the
    first real event, which for an idle workload could be minutes.
    """
    return f"retry: {RETRY_MS}\n\n: open\n\n"
