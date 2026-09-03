"""Build controller settings: the shared connection identity plus the loop's pacing."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field

from common.config import LoopSettings


class BuildControllerSettings(LoopSettings):
    """Controller settings: the shared connection and pacing settings plus its own.

    ``resync_seconds`` is how long each watch is held open, and so also the
    relist interval - the stream ending is what starts the next resync.
    """

    app_name: str = "serverless-build-controller"

    # Tag GC: prune the per-build tags kpack accumulates in this region's
    # registry (docs/BUILD-CONTROLLER.md - Registry tag GC). Also needs the registry
    # API token; without one the GC announces itself off and does nothing.
    gc_enabled: bool = True
    # Sweep interval, hours-scale rather than the resync's minutes; each sweep
    # re-derives what to delete from live state.
    gc_interval_seconds: int = Field(default=21600, gt=0)
    # Newest per-build tags kept beyond the protected ones; mirrors
    # build.history.success's default of 3.
    gc_keep_builds: int = Field(default=3, ge=0)


@lru_cache
def get_settings() -> BuildControllerSettings:
    """Cached settings singleton."""
    return BuildControllerSettings()
