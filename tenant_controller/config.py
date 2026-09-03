"""Tenant controller settings: the shared connection identity plus the loop's pacing."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field

from common.config import LoopSettings


class TenantControllerSettings(LoopSettings):
    """Tenant controller settings: the shared connection and pacing settings plus its own."""

    app_name: str = "serverless-tenant-controller"

    # The internal provision API (no Route, no Ingress - a ClusterIP Service
    # the API's namespace reaches).
    port: int = 8080

    # The tenant-templates ConfigMap mount. Whole-ConfigMap, never subPath:
    # subPath mounts are not refreshed, and the refresh is how a helm upgrade
    # reaches this loop.
    templates_dir: str = "/etc/serverless/tenant-templates"

    # Every Nth pass converges even stamp-matching namespaces, repairing
    # drift in the objects themselves (a deleted NetworkPolicy does not
    # change the stamp). Roughly hourly at the default resync.
    full_resync_passes: int = Field(default=12, gt=0)

    # A reconcile pass runs its converges on a thread pool of this size. They
    # are independent per namespace, since the stamp is per namespace.
    converge_workers: int = Field(default=4, ge=1)

    # The provision API's own pool, separate from the loop's. It bounds how
    # many converges a burst of creates has in flight, and keeps a slow region
    # off the server's request threads, where it would starve the probes.
    provision_workers: int = Field(default=8, ge=1)

    # Namespace GC: collect tenant namespaces that have stayed empty of
    # workloads for the whole grace period. Deletion is off unless an operator
    # turns it on.
    gc_enabled: bool = False
    gc_interval_seconds: int = Field(default=3600, gt=0)
    gc_grace_seconds: int = Field(default=86400, gt=0)


@lru_cache
def get_settings() -> TenantControllerSettings:
    """Cached settings singleton."""
    return TenantControllerSettings()
