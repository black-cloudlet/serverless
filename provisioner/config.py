"""Provisioner settings: the shared connection identity plus the loop's pacing."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field

from common.config import LoopSettings


class ProvisionerSettings(LoopSettings):
    """Provisioner settings: the shared connection and pacing settings plus its own."""

    app_name: str = "serverless-provisioner"

    # The internal ensure API (no Route, no Ingress - a ClusterIP Service the
    # API's namespace reaches).
    port: int = 8080
    # Shared secret the ensure call must present, from Vault via ESO. Empty
    # disables the check: the NetworkPolicy is the primary control, and a dev
    # cluster has no Vault to take a token from.
    provisioner_token: str = ""

    # The tenant-templates ConfigMap mount. Whole-ConfigMap, never subPath:
    # subPath mounts are not refreshed, and the refresh is how a helm upgrade
    # reaches this loop.
    templates_dir: str = "/etc/serverless/tenant-templates"

    # Every Nth pass converges even stamp-matching namespaces, repairing
    # drift in the objects themselves (a deleted NetworkPolicy does not
    # change the stamp). Roughly hourly at the default resync.
    full_resync_passes: int = Field(default=12, gt=0)

    # Converges run on a thread pool of this size: independent per namespace
    # (the stamp is per namespace), so a template rollout over many tenants
    # is bounded by the pool, not serialized.
    converge_workers: int = Field(default=4, ge=1)

    # The ensure API's own pool, separate from the loop's. It is the bound on
    # how many converges a burst of creates can have in flight, and the reason
    # a slow region cannot reach the server's threads and starve the probes.
    ensure_workers: int = Field(default=8, ge=1)


@lru_cache
def get_settings() -> ProvisionerSettings:
    """Cached settings singleton."""
    return ProvisionerSettings()
