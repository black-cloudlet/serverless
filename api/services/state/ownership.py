"""Whether a fetched workload is the caller's to see.

One rule, in one place. Every read path asks it - the single GET, the stats
view, the log snapshot, the update's state load, and the delete - and each does
something different when the answer is no, which is why what is shared here is
the predicate rather than the whole check.

Not in :mod:`api.services.state.ksvc_state`: that module is pure interpretation of a
Kubernetes object, and this one weighs the object against a caller.
"""

from __future__ import annotations

from cloudlet_apis.auth import Principal

from api.models.common import LABEL_GROUP, LABEL_OFFERING


def owned_by(obj: dict, user: Principal, offering: str) -> bool:
    """Whether ``obj`` belongs to a group ``user`` may act for, in this offering.

    Both halves matter. The group is the tenancy boundary. The offering is
    checked too because the object name is ``{name}-{group}`` for both of them,
    so a container could otherwise be reached through the functions path.

    A caller who fails this must be told the workload does not exist rather than
    that they may not have it - the callers do that, since a 404 is right for a
    read and a delete has to record the site instead.

    Args:
        obj: The workload's KSVC, already fetched.
        user: The authenticated caller.
        offering: The offering the request came in through ("function"/"container").

    Returns:
        True if the caller may act on this workload.
    """
    labels = (obj.get("metadata", {}) or {}).get("labels", {}) or {}
    return user.can_access_group(labels.get(LABEL_GROUP, "")) and (
        labels.get(LABEL_OFFERING) == offering
    )
