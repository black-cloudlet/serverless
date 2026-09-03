"""Provisioning a group's namespace on demand, in every region at once.

The reconcile loop is local-only and level-triggered; provisioning is the other
half. The API calls it before a workload deploys, and it is the one path that
reaches **both** clusters, so a create can land in a region whose namespace the
local loop has not made yet.

A peer's namespace is written from *this* pod's template set. The set is
region-neutral - both regions render it from the same chart and the same
values, so the bytes, and therefore the hash, are identical. While Argo has
one cluster synced ahead of the other the two sets differ; the peer's own loop
then pulls its namespace back to the set that cluster holds, and that ends
when the sync lands.

Nothing here is exclusive with the loop: both call the same idempotent
``converge`` under the same field manager, so the two racing on one namespace
converge it twice, not halfway.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from concurrent.futures import Executor
from dataclasses import dataclass

from cloudlet_apis.logging import get_logger

from common.cluster import Cluster
from common.config import PROVISION_FAILED, PROVISION_READY, PROVISION_TIMEOUT
from tenant_controller.reconcile import converge_if_stale
from tenant_controller.templates import TemplateSet

logger = get_logger(__name__)


@dataclass(frozen=True)
class RegionOutcome:
    """What provisioning did in one region.

    Attributes:
        region: The region name.
        status: ``Ready``, ``Failed`` or ``Timeout``.
        message: The failure detail, or None when it converged.
    """

    region: str
    status: str
    message: str | None = None

    @property
    def ok(self) -> bool:
        """Whether this region converged."""
        return self.status == PROVISION_READY


async def provision(
    clusters: Sequence[Cluster],
    namespace: str,
    group: str,
    templates: TemplateSet,
    *,
    timeout: float,
    executor: Executor,
) -> list[RegionOutcome]:
    """Converge ``namespace`` in every region concurrently, one row per region.

    A region that fails or times out yields its own row instead of aborting
    the others; the caller decides what a partial result means, the same way
    it does for a partial deploy (docs/API.md - Partial-failure
    semantics).

    Async, and the blocking converges run on a *caller-owned* pool rather than
    on the event loop or the server's request threads. The pool's size is the
    bound on how much converging is in flight at once.

    Args:
        clusters: The clusters to converge in, one per region.
        namespace: The tenant namespace (derived and validated by the caller).
        group: The owning (normalized) group.
        templates: The currently mounted template set.
        timeout: Budget for the whole fan-out, not per region.
        executor: The pool the converges run on.

    Returns:
        One outcome per cluster, in the order given.

    Raises:
        ValueError: If no clusters were given.
    """
    if not clusters:
        raise ValueError("no regions are configured")
    loop = asyncio.get_running_loop()
    pending = [
        (
            cluster.region,
            loop.run_in_executor(executor, converge_if_stale, cluster, namespace, group, templates),
        )
        for cluster in clusters
    ]
    # One wait covering all of them: the timeout is the fan-out's budget, so
    # two dead regions cost one deadline between them, not one each.
    await asyncio.wait([future for _region, future in pending], timeout=timeout)
    return [_outcome(region, future, timeout) for region, future in pending]


def _outcome(region: str, future: asyncio.Future, timeout: float) -> RegionOutcome:
    """Read one region's finished converge, or report why it is not finished."""
    if not future.done():
        # Cancel drops it if the pool has not started it; one already running
        # cannot be interrupted and is left to finish. Either way the caller
        # gets its answer now, and an abandoned converge is safe to lose - the
        # stamp is written last, so the next pass redoes the namespace.
        future.cancel()
        future.add_done_callback(_discard)
        logger.warning("provisioning in region %s timed out after %ss", region, timeout)
        return RegionOutcome(
            region, PROVISION_TIMEOUT, f"region unreachable (timed out after {timeout}s)"
        )
    error = future.exception()
    if error is not None:
        logger.error("provisioning in region %s failed: %s", region, error)
        return RegionOutcome(region, PROVISION_FAILED, str(error))
    return RegionOutcome(region, PROVISION_READY)


def _discard(future: asyncio.Future) -> None:
    """Retrieve an abandoned converge's exception, so asyncio does not log it."""
    if not future.cancelled():
        future.exception()
