"""Stream tickets: how a browser authenticates a stream it cannot add a header to.

``EventSource`` is the only way a browser consumes Server-Sent Events, and it
sends no ``Authorization`` header - there is no API to give it one. That leaves
the credential in the URL, and putting the SSO token there is the thing to
avoid: it is valid against every endpoint, it outlives the request by its whole
expiry, and a URL is written to the Route's access log, this API's own log line
and the user's history.

So the token buys a ticket instead. The caller mints one with the bearer token it
already has, over an ordinary POST that can carry a header, and opens the stream
with that. What the ticket is worth is deliberately almost nothing: one stream
path, for about a minute, carrying an identity the caller already had.

It is signed rather than stored. Two replicas serve behind one Route and either
may take the stream, so a ticket kept in the memory of the process that minted it
would fail roughly half the time; an HMAC over the payload needs no shared state
to verify, only the shared key.
"""

from __future__ import annotations

import base64
import hmac
import json
import time
from hashlib import sha256

from cloudlet_apis.auth import Principal
from cloudlet_apis.errors import ServiceUnavailableError, UnauthenticatedError

# Bumped if the payload's shape changes, so an old ticket is rejected rather
# than half-understood. Tickets live for a minute, so a version is only ever in
# use during one deploy's overlap.
_VERSION = 1


def _b64(raw: bytes) -> str:
    """URL-safe base64 without padding (a ticket travels in a query string)."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    """Reverse :func:`_b64`, restoring the padding it stripped."""
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class StreamTicket:
    """A minted ticket and when it stops being one.

    Attributes:
        value: The opaque string to send as ``?ticket=``.
        expires_at: Unix time after which it is refused.
    """

    def __init__(self, value: str, expires_at: int):
        """Initialize the minted ticket."""
        self.value = value
        self.expires_at = expires_at


class StreamTickets:
    """Mints and verifies stream tickets for one deployment.

    Attributes:
        ttl_seconds: How long a minted ticket stays valid.
    """

    def __init__(self, key: str, *, ttl_seconds: int = 60):
        """Initialize from the configured signing key.

        Args:
            key: The HMAC key (``SERVERLESS_STREAM_TICKET_KEY``, from Vault via
                ESO). Empty disables minting - the same way an empty admin key
                disables key auth, rather than falling back to something
                guessable. The streams still take the Authorization header, so
                this only turns off the browser path.
            ttl_seconds: Ticket lifetime. Short by design: a ticket is spent
                opening one connection, and the stream it opened lives on its
                own afterwards, so nothing needs it to last.
        """
        self._key = key.encode("utf-8")
        self.ttl_seconds = ttl_seconds

    @property
    def enabled(self) -> bool:
        """Whether a signing key is configured."""
        return bool(self._key)

    def _sign(self, payload: bytes) -> str:
        """The signature over an encoded payload."""
        return _b64(hmac.new(self._key, payload, sha256).digest())

    def mint(self, principal: Principal, path: str) -> StreamTicket:
        """Issue a ticket for ``principal`` to open ``path``.

        The path is inside the signature, so a ticket for one workload's logs
        cannot be replayed against another's - which matters because a ticket,
        unlike a header, is visible to anything that sees the URL.

        Args:
            principal: The already-authenticated caller, carried so the stream
                resolves the same identity the mint did.
            path: The exact request path the ticket may open.

        Returns:
            The minted ticket.

        Raises:
            ServiceUnavailableError: If no signing key is configured.
        """
        if not self.enabled:
            raise ServiceUnavailableError(
                "stream tickets are not configured on this deployment; open the stream "
                "with an Authorization header instead"
            )
        expires_at = int(time.time()) + self.ttl_seconds
        payload = _b64(
            json.dumps(
                {
                    "v": _VERSION,
                    "sub": principal.subject,
                    "usr": principal.username,
                    "grp": principal.groups,
                    "adm": principal.is_admin,
                    "pth": path,
                    "exp": expires_at,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        return StreamTicket(f"{payload}.{self._sign(payload.encode('ascii'))}", expires_at)

    def verify(self, ticket: str, path: str) -> Principal:
        """Resolve a ticket back to its Principal, or refuse it.

        Every failure raises the same error with the same message. Telling a
        caller whether a ticket was expired, forged or minted for somewhere else
        would help exactly one kind of caller - the one probing.

        Args:
            ticket: The ``?ticket=`` value.
            path: The path being opened, which must be the one it was minted for.

        Returns:
            The caller the ticket was minted for.

        Raises:
            UnauthenticatedError: If the ticket is malformed, unsigned by us,
                expired, or was minted for a different path.
        """
        invalid = UnauthenticatedError("Invalid or expired stream ticket.")
        if not self.enabled:
            raise invalid
        payload, _, signature = ticket.partition(".")
        if not payload or not signature:
            raise invalid
        # Constant-time, and before the payload is decoded: a forged ticket must
        # not reach the parser at all.
        if not hmac.compare_digest(signature, self._sign(payload.encode("ascii"))):
            raise invalid
        try:
            claims = json.loads(_unb64(payload))
        except (ValueError, TypeError) as exc:
            raise invalid from exc
        if not isinstance(claims, dict) or claims.get("v") != _VERSION:
            raise invalid
        if claims.get("pth") != path:
            raise invalid
        # Compared against the real clock, not a truncated one: `exp` is whole
        # seconds, so testing it with an int `now` would leave a ticket valid for
        # up to a second past its expiry - a meaningful slice of a 60s lifetime.
        if not isinstance(claims.get("exp"), int) or claims["exp"] <= time.time():
            raise invalid
        groups = claims.get("grp")
        return Principal(
            subject=str(claims.get("sub", "")),
            username=str(claims.get("usr", "")),
            groups=groups if isinstance(groups, dict) else {},
            is_admin=bool(claims.get("adm")),
        )
