"""Server-Sent Events framing: the wire format, and the event the services yield.

Kept apart from the streams themselves so the services stay in the business of
producing *events* and only the router turns them into bytes. That split is what
lets the stream tests assert on typed events instead of parsing text, and it is
the same shape as the rest of the API, where a service returns a model and the
framework renders it.

The format is the one the EventSource specification defines: ``field: value``
lines, a blank line ending each event, and a leading ``:`` marking a comment
that carries no event at all - which is what a heartbeat is.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

MEDIA_TYPE = "text/event-stream"

# Set on the response, not guessed at by the proxy in front of it. `no-transform`
# and `X-Accel-Buffering` both say the same thing to two different intermediaries:
# do not buffer this, because a buffered event stream is indistinguishable from a
# hung one until the buffer happens to flush.
HEADERS = {
    "Cache-Control": "no-cache, no-store, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

# How long a client waits before reconnecting, sent once at the top of a stream.
# A stream ends on its own every `max_seconds` (see StreamConfig), so this is not
# an error path - it is the normal cadence, and the default of 3s would have every
# client reconnecting three seconds after every scheduled close.
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
    # One `data:` line per line of payload. JSON from pydantic carries no
    # newlines today, but the rule is the format's, not the payload's: a single
    # embedded newline would otherwise silently truncate the event.
    lines = "".join(f"data: {line}\n" for line in body.split("\n"))
    return f"event: {event.name}\n{lines}\n"


def preamble() -> str:
    """The bytes sent before the first event.

    Carries the reconnection delay, and - being a comment - flushes the response
    headers through any intermediary that would otherwise hold them until the
    first real event, which for an idle workload could be minutes.
    """
    return f"retry: {RETRY_MS}\n\n: open\n\n"
