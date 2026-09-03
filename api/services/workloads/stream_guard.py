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
    it. Every ending releases it: ``aclose`` releases after closing ``inner``,
    exhaustion and failure release from ``__anext__``, and a
    ``weakref.finalize`` backstop releases for an object the response layer
    dropped without ever closing or iterating it. Release is idempotent, so the
    several owners cannot double-free
    (docs/ARCHITECTURE.md - A held-open stream holds a thread).
    """

    def __init__(self, slot: StreamSlot, inner: AsyncGenerator[StreamEvent | str, None]):
        self._slot = slot
        self._inner = inner
        # Bound to the slot only: a reference to `self` would keep this object
        # alive and the finalizer would never fire.
        weakref.finalize(self, slot.release)

    def __aiter__(self) -> _SlotGuardedStream:
        return self

    async def __anext__(self) -> StreamEvent | str:
        try:
            return await self._inner.__anext__()
        except BaseException:
            # StopAsyncIteration included: however the stream ends, the slot
            # goes back here rather than on a later aclose.
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
