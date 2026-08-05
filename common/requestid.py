"""Per-request correlation id: adopt an inbound ``X-Request-ID`` or mint one.

Shared web infra - the API (and the future builder) want the same request
correlation. OpenShift's router already stamps an ``X-Request-ID`` on every
proxied request; we adopt it when present (so the id matches the router/ingress
logs end to end) and generate a UUID otherwise. The id is exposed three ways:

- ``request.state.request_id`` - for handlers, e.g. the error envelope;
- the ``X-Request-ID`` response header - echoed back to the caller;
- a context var - so log records can carry it (see ``common.logging``).
"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "x-request-id"

# Bounds an adopted inbound id so a hostile upstream header cannot inject
# control characters into logs. Anything outside it gets a fresh id.
_VALID_ID = re.compile(r"^[A-Za-z0-9._~+/=@-]{1,128}$")

# "-" reads clearly in a log line for work with no request scope (startup, etc.).
_NO_REQUEST = "-"
_request_id: ContextVar[str] = ContextVar("request_id", default=_NO_REQUEST)


def get_request_id() -> str:
    """The current request's correlation id, or ``"-"`` outside a request."""
    return _request_id.get()


def _adopt_or_mint(inbound: str | None) -> str:
    """Reuse a well-formed inbound id, else mint a fresh UUID4 hex."""
    if inbound and _VALID_ID.match(inbound):
        return inbound
    return uuid.uuid4().hex


class RequestIDMiddleware:
    """ASGI middleware assigning a correlation id to every HTTP request.

    Pure ASGI (not ``BaseHTTPMiddleware``) so the context var set here propagates
    into the endpoint and its log records without a task hop.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the downstream ASGI app.

        Args:
            app: The next ASGI application in the stack.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Assign/propagate the id, expose it, and echo it on the response."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        raw = headers.get(REQUEST_ID_HEADER.encode())
        request_id = _adopt_or_mint(raw.decode("latin-1") if raw else None)

        # Expose on request.state (Starlette reads state from scope["state"]).
        scope.setdefault("state", {})["request_id"] = request_id
        token = _request_id.set(request_id)

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                # Drop any upstream copy, then set ours (single, canonical value).
                headers = [
                    (k, v)
                    for (k, v) in message.get("headers") or []
                    if k.lower() != REQUEST_ID_HEADER.encode()
                ]
                headers.append((REQUEST_ID_HEADER.encode(), request_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            _request_id.reset(token)
