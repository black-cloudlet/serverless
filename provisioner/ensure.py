"""Converging one group on demand, in every region at once.

The reconcile loop is deliberately local-only and level-triggered. Ensure is
the other half: the kick the API sends before a workload deploys, and the one
path that must reach **both** clusters - a create landing in the region whose
namespace does not exist yet is exactly what a per-cluster loop cannot fix in
time.

Writing a peer's namespace from *this* pod's template set is sound only
because the set is region-neutral: both regions render it from the same chart
and the same values, so the bytes - and therefore the hash - are identical.
The exception is the gap while Argo syncs one cluster ahead of the other,
where the peer's own loop will pull its namespace back to the set that
cluster actually holds. That is a legitimate convergence, it ends when the
sync lands, and it is why a per-region value must never enter the set.

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
from provisioner.reconcile import converge
from provisioner.templates import TemplateSet

logger = get_logger(__name__)

# The per-region vocabulary, the same words the API's RegionStatus uses: the
# ensure rows are rendered beside deploy rows in the same response.
READY = "Ready"
FAILED = "Failed"
TIMEOUT = "Timeout"


@dataclass(frozen=True)
class RegionOutcome:
    """What ensure did in one region.

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
        return self.status == READY


async def ensure(
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
    the others: the caller decides what a partial ensure means, exactly as it
    does for a partial deploy.

    Async, and the blocking converges run on a *caller-owned* pool: the
    request thread is never held, so a slow region cannot consume the server's
    threads and starve the probes, and the pool's size is the one bound on how
    much converging is in flight at once.

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
            loop.run_in_executor(executor, converge, cluster, namespace, group, templates),
        )
        for cluster in clusters
    ]
    # One wait for all of them, so the budget is the fan-out's: two dead
    # regions cost one deadline, not one deadline each.
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
        logger.warning("ensure in region %s timed out after %ss", region, timeout)
        return RegionOutcome(region, TIMEOUT, f"region unreachable (timed out after {timeout}s)")
    error = future.exception()
    if error is not None:
        logger.error("ensure in region %s failed: %s", region, error)
        return RegionOutcome(region, FAILED, str(error))
    return RegionOutcome(region, READY)


def _discard(future: asyncio.Future) -> None:
    """Retrieve an abandoned converge's exception, so asyncio does not log it."""
    if not future.cancelled():
        future.exception()
