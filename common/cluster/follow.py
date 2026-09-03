"""A held-open pod log stream, decoupled from the client that opened it."""

from __future__ import annotations

from collections.abc import Iterator


class LogFollow:
    """A held-open pod log stream: lines as they arrive, and a way to end it.

    Two threads touch one of these and they do different things. The worker
    thread iterates :meth:`lines`, blocked on the socket in between. The event
    loop calls :meth:`close`, which ends that block by closing the socket: the
    pending read fails, the iteration stops, and the thread is returned to the
    pool. The ``_closed`` flag alone does not end it, being read only between
    lines (docs/STREAMING.md - A held-open stream holds a thread).
    """

    def __init__(self, response):
        """Wrap the raw streaming response.

        Args:
            response: The urllib3 response from a ``follow=True`` log read.
        """
        self._response = response
        self._closed = False

    # A line still waiting for its newline is held in memory; past this many
    # bytes it is emitted as if the newline had arrived, so a container writing
    # without newlines cannot grow the buffer without bound.
    MAX_LINE_BYTES = 1024 * 1024

    def lines(self) -> Iterator[str]:
        """Yield complete log lines as the container writes them (blocking).

        The API server chunks the transfer at whatever boundary it likes, so a
        single write can be split across chunks and several can share one:
        chunks are reassembled here into whole lines.

        Yields:
            One log line at a time, newline stripped. A trailing partial line is
            emitted when the stream ends, so a final write with no newline is
            not swallowed; a line longer than :attr:`MAX_LINE_BYTES` is emitted
            in pieces rather than held.
        """
        buffer = ""
        # A bounded amt: with amt=None urllib3 only yields per-chunk on a
        # chunked-transfer response; anything that de-chunks (a proxy, HTTP/2)
        # would degrade it to read-to-EOF - which for a follow never comes.
        for chunk in self._response.stream(amt=2**16, decode_content=True):
            if self._closed:
                break
            buffer += chunk.decode("utf-8", errors="replace")
            # Not splitlines(): it also breaks on \v, \f and U+2028, any of which
            # a workload may legitimately log inside one line.
            *complete, buffer = buffer.split("\n")
            for line in complete:
                yield line.rstrip("\r")
            if len(buffer) > self.MAX_LINE_BYTES:
                yield buffer
                buffer = ""
        if buffer and not self._closed:
            yield buffer.rstrip("\r")

    def close(self) -> None:
        """End the stream, unblocking a thread waiting on it. Idempotent.

        ``close`` drops the socket, which interrupts the pending read;
        ``release_conn`` stops the pool holding a connection that will never be
        reused. Failures are swallowed; this runs only during teardown.
        """
        self._closed = True
        for end in (self._response.close, self._response.release_conn):
            try:
                end()
            except Exception:  # noqa: BLE001, S110 - teardown; nothing to report to
                pass
