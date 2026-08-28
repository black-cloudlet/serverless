"""Provisioner settings: the shared connection identity plus the loop's pacing."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field

from common.config import CommonSettings
from common.names import NAMESPACE_SUFFIX


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

    # Tenant-namespace suffix (common.names.namespace_for_group) - the same
    # one function the API uses, so the two never derive different names.
    namespace_suffix: str = NAMESPACE_SUFFIX


@lru_cache
def get_settings() -> ProvisionerSettings:
    """Cached settings singleton."""
    return ProvisionerSettings()
