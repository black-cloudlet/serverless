"""Build controller settings: the shared connection identity plus the loop's pacing."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field

from common.config import LoopSettings


class ControllerSettings(LoopSettings):
    """Controller settings: the shared connection and pacing settings plus its own.

    ``resync_seconds`` is how long each watch is held open, and so also the
    relist interval - the stream ending is what starts the next resync.
    """

    app_name: str = "serverless-build-controller"

    # Tag GC: prune the per-build tags kpack accumulates in this region's
    # registry (docs/BUILDING.md - Registry tag GC). Also needs the registry
    # API token; without one the GC announces itself off and does nothing.
    gc_enabled: bool = True
    # Hours-scale, not the resync's minutes: garbage accrues one tag per build,
    # and each sweep re-derives everything, so nothing is lost by waiting.
    gc_interval_seconds: int = Field(default=21600, gt=0)
    # Newest per-build tags kept beyond the protected ones, so recent builds
    # stay addressable - mirroring build.history.success's default of 3.
    gc_keep_builds: int = Field(default=3, ge=0)


@lru_cache
def get_settings() -> ControllerSettings:
    """Cached settings singleton."""
    return ControllerSettings()
