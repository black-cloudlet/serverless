"""Provisioner settings: the shared connection identity plus the loop's pacing."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field

from common.config import CommonSettings


class ProvisionerSettings(CommonSettings):
    """Provisioner settings: the shared connection settings plus the loop's own."""

    app_name: str = "serverless-provisioner"

    # The pause between reconcile passes: periodic, no watch - convergence
    # within minutes is enough, and a relist per pass self-heals a crash.
    resync_seconds: int = Field(default=300, gt=0)
    # Only for a pass that raised; a clean pass waits the resync interval.
    error_backoff_seconds: float = Field(default=5.0, ge=0)

    # The tenant-templates ConfigMap mount. Whole-ConfigMap, never subPath:
    # subPath mounts are not refreshed, and the refresh is how a helm upgrade
    # reaches this loop.
    templates_dir: str = "/etc/serverless/tenant-templates"

    # Every Nth pass converges even stamp-matching namespaces, repairing
    # drift in the objects themselves (a deleted NetworkPolicy does not
    # change the stamp). Roughly hourly at the default resync.
    full_resync_passes: int = Field(default=12, gt=0)


@lru_cache
def get_settings() -> ProvisionerSettings:
    """Cached settings singleton."""
    return ProvisionerSettings()
