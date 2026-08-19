"""Tie a stream's admission slot to the generator that streams it."""

from __future__ import annotations

import weakref
from collections.abc import AsyncGenerator

from cloudlet_apis.logging import get_logger

from api.services.streams.capacity import StreamSlot
from api.services.streams.sse import StreamEvent

logger = get_logger(__name__)


class _SlotGuardedStream:
    """``inner``'s events, with ``slot`` released however the stream ends.

    The slot is held for the life of this object, not of the call that admitted
    it - which is why it cannot simply be a ``with``. An object rather than a
    wrapping generator, deliberately: closing a *never-started* generator skips
    its body, so a ``finally`` in one cannot cover the stream that is handed to
    the response layer and then never iterated (a client that disconnects
    before the body begins) - exactly the stream whose slot would otherwise be
    gone until a restart. Here ``aclose`` always runs, exhaustion and failure
    release directly, and the ``weakref.finalize`` backstop covers an object
    the response layer dropped without closing. Release is idempotent, so the
    several owners cannot double-free.
    """

    def __init__(self, slot: StreamSlot, inner: AsyncGenerator[StreamEvent | str, None]):
        self._slot = slot
        self._inner = inner
        # Bound to the slot only - a reference to `self` here would keep this
        # object alive forever and the finalizer from ever firing.
        weakref.finalize(self, slot.release)

    def __aiter__(self) -> _SlotGuardedStream:
        return self

    async def __anext__(self) -> StreamEvent | str:
        try:
            return await self._inner.__anext__()
        except BaseException:
            # StopAsyncIteration included: however the stream ends, the slot
            # goes back now, not when the caller remembers to aclose.
            self._slot.release()
            raise

    async def aclose(self) -> None:
        try:
            await self._inner.aclose()
        finally:
            self._slot.release()


def _slot_guarded(
    slot: StreamSlot, inner: AsyncGenerator[StreamEvent | str, None]
) -> _SlotGuardedStream:
    """Wrap ``inner`` so ``slot`` is released however the stream ends."""
    return _SlotGuardedStream(slot, inner)
