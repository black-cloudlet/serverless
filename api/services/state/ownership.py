"""Whether a fetched workload is the caller's to see.

Not in :mod:`api.services.state.ksvc_state`: that module is pure interpretation of a
Kubernetes object, and this one weighs the object against a caller.
"""

from __future__ import annotations

from api.auth.claims import Principal
from api.models.common import LABEL_GROUP, LABEL_OFFERING


def owned_by(obj: dict, user: Principal, offering: str) -> bool:
    """Whether ``obj`` belongs to a group ``user`` may act for, in this offering."""
    labels = (obj.get("metadata", {}) or {}).get("labels", {}) or {}
    return user.can_access_group(labels.get(LABEL_GROUP, "")) and (
        labels.get(LABEL_OFFERING) == offering
    )
