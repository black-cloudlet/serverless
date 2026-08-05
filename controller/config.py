"""Build controller settings.

The connection identity - sites, client cert, CA bundle, timeouts - is shared
and lives in :mod:`common.config`; this module adds the loop's own pacing.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field

from common.config import CommonSettings


class ControllerSettings(CommonSettings):
    """Controller settings: the shared connection settings plus the loop's own."""

    app_name: str = "serverless-build-controller"

    # How long the server holds each watch open. It doubles as the resync
    # interval: the stream ending is what sends the loop back through a full
    # relist, so one value paces both (docs/BUILDING.md - Digest propagation).
    resync_seconds: int = Field(default=300, gt=0)
    # Applied only when a resync itself fails - a watch that ends is routine and
    # is followed immediately by another relist.
    error_backoff_seconds: float = Field(default=5.0, ge=0)


@lru_cache
def get_settings() -> ControllerSettings:
    """Cached settings singleton."""
    return ControllerSettings()
