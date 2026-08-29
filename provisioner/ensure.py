"""Converging one group on demand, in every region at once.

The reconcile loop is deliberately local-only and level-triggered. Ensure is
the other half: the kick the API sends before a workload deploys, and the one
path that must reach **both** clusters - a create landing in the region whose
namespace does not exist yet is exactly what a per-cluster loop cannot fix in
time.

Nothing here is exclusive with the loop: both call the same idempotent
``converge`` under the same field manager, so the two racing on one namespace
converge it twice, not halfway.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
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


def ensure(
    clusters: Sequence[Cluster],
    namespace: str,
    group: str,
    templates: TemplateSet,
    *,
    timeout: float,
) -> list[RegionOutcome]:
    """Converge ``namespace`` in every region concurrently, one row per region.

    A region that fails or times out yields its own row instead of aborting
    the others: the caller decides what a partial ensure means, exactly as it
    does for a partial deploy.

    Args:
        clusters: The clusters to converge in, one per region.
        namespace: The tenant namespace (derived and validated by the caller).
        group: The owning (normalized) group.
        templates: The currently mounted template set.
        timeout: Budget for the whole fan-out, not per region.

    Returns:
        One outcome per cluster, in the order given.

    Raises:
        ValueError: If no clusters were given.
    """
    if not clusters:
        raise ValueError("no regions are configured")
    deadline = time.monotonic() + timeout
    pool = ThreadPoolExecutor(max_workers=len(clusters))
    try:
        futures = [
            (cluster.region, pool.submit(converge, cluster, namespace, group, templates))
            for cluster in clusters
        ]
        return [_outcome(region, future, deadline, timeout) for region, future in futures]
    finally:
        # wait=False: a converge that blew the deadline holds a thread we
        # cannot interrupt, and the caller is owed its answer now. The write
        # it is in the middle of is safe to abandon - the stamp is written
        # last, so the next pass redoes the namespace.
        pool.shutdown(wait=False, cancel_futures=True)


def _outcome(region: str, future: Future, deadline: float, timeout: float) -> RegionOutcome:
    """Wait for one region's converge, against the fan-out's shared deadline."""
    try:
        future.result(timeout=max(deadline - time.monotonic(), 0.0))
    except FutureTimeout:
        logger.warning("ensure in region %s timed out after %ss", region, timeout)
        return RegionOutcome(region, TIMEOUT, f"region unreachable (timed out after {timeout}s)")
    except Exception as exc:  # noqa: BLE001 - surfaced as this region's row
        logger.exception("ensure in region %s failed", region)
        return RegionOutcome(region, FAILED, str(exc))
    return RegionOutcome(region, READY)
