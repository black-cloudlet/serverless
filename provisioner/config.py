"""Provisioner settings: the shared connection identity plus the loop's pacing."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field

from common.config import CommonSettings
from common.names import NAMESPACE_SUFFIX


class ProvisionerSettings(CommonSettings):
    """Provisioner settings: the shared connection settings plus the loop's own."""

    app_name: str = "serverless-provisioner"

    # The pause between reconcile passes. There is no watch to hold open: the
    # loop is periodic because its triggers - a synced templates ConfigMap, a
    # namespace created by an ensure call - need convergence within minutes,
    # not milliseconds, and a relist per pass is what makes a crashed pass
    # self-healing.
    resync_seconds: int = Field(default=300, gt=0)
    # Only for a pass that raised; a clean pass waits the resync interval.
    error_backoff_seconds: float = Field(default=5.0, ge=0)

    # Where the chart mounts the tenant-templates ConfigMap. A whole-ConfigMap
    # mount, never subPath: subPath mounts are not refreshed by the kubelet,
    # and the refresh is how a helm upgrade reaches this loop.
    templates_dir: str = "/etc/serverless/tenant-templates"

    # Suffixes every tenant namespace (common.names.namespace_for_group). A
    # value here so the chart can set it, and shared with the API through the
    # same one function - the two must never derive different names.
    namespace_suffix: str = NAMESPACE_SUFFIX


@lru_cache
def get_settings() -> ProvisionerSettings:
    """Cached settings singleton."""
    return ProvisionerSettings()
