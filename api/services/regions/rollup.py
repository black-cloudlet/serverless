"""Rolling per-region results up into one status, and that status into HTTP.

The single definition of the rollup, shared by the create path (aggregate) and
the read paths (overall_status), so the two cannot drift. Pure functions - the
fan-out that produces the per-region statuses lives in
:mod:`api.services.regions.deployer`.
"""

from __future__ import annotations

from api.models.common import RegionStatus
from common.errors import RegionTotalFailure


def aggregate(statuses: list[RegionStatus]) -> str:
    """Overall status for the create/update path.

    Raises RegionTotalFailure when every region failed; otherwise delegates the rollup
    to overall_status, mapping an unreachable region to ``Failed``.

    Args:
        statuses: The per-region results of the apply fan-out.

    Returns:
        The overall status (Ready/Deploying/Failed).

    Raises:
        RegionTotalFailure: If every region failed.
    """
    if all(s.message is not None for s in statuses):
        raise RegionTotalFailure(
            "Deployment failed in all regions.",
            details=[{"region": s.region, "message": s.message} for s in statuses],
        )
    return overall_status_for_regions(statuses)


def overall_status_for_regions(statuses: list[RegionStatus]) -> str:
    """Roll up RegionStatus objects, mapping an unreachable region to ``Failed``.

    The projection shared by the create path (:func:`aggregate`) and the GET read
    path.

    Args:
        statuses: The per-region statuses.

    Returns:
        The overall status (Ready/Deploying/Failed).
    """
    return overall_status([s.status if s.message is None else "Failed" for s in statuses])


def overall_status(statuses: list[str]) -> str:
    """Collapse per-region KSVC statuses into one overall status (GET / list).

    A ``Failed`` region makes the whole deployment ``Failed``; a ``Terminating`` one
    makes it ``Terminating``. Otherwise all-``Ready`` is ``Ready`` and anything in
    flight is ``Deploying`` - including mixed ``Ready`` + ``Deploying``, a normal
    rollout with one region ahead, NOT a failure
    (docs/ARCHITECTURE.md - Partial-failure semantics).

    Args:
        statuses: The per-region status strings.

    Returns:
        The overall status (Ready/Deploying/Failed/Terminating).
    """
    if not statuses:
        return "Failed"
    if any(s == "Failed" for s in statuses):
        return "Failed"
    if any(s == "Terminating" for s in statuses):
        return "Terminating"
    if all(s == "Ready" for s in statuses):
        return "Ready"
    return "Deploying"


def status_code_for(overall: str, created: bool) -> int:
    """Map an overall status to an HTTP status code.

    Args:
        overall: The rolled-up status (Ready/Deploying/Failed).
        created: Whether the call created a new workload (vs updated one).

    Returns:
        207 for Failed, 202 for Deploying/Building, 201 for a create, else 200.
    """
    if overall == "Failed":
        return 207
    if overall in ("Deploying", "Building"):
        return 202  # accepted, still in flight - a non-terminal poll state
    return 201 if created else 200
