"""Ownership labels stamped on every managed resource (shared by all services)."""

from __future__ import annotations

LABEL_GROUP = "serverless.platform/group"
LABEL_MANAGED_BY = "serverless.platform/managed-by"
LABEL_OWNER = "serverless.platform/owner"
LABEL_OFFERING = "serverless.platform/offering"
LABEL_WORKLOAD = "serverless.platform/workload"
MANAGED_BY_VALUE = "serverless-api"

# The two values LABEL_OFFERING takes. They live beside the key rather than in
# the API's service layer because they are the same string in three places - the
# label, the API kind in the URL path, and the response `type` - and a service
# that only reads labels (the build service) still has to know them.
OFFERING_FUNCTION = "function"
OFFERING_CONTAINER = "container"


def ownership_labels(group: str, owner: str, offering: str | None = None) -> dict[str, str]:
    """Build the ownership/management labels stamped on every resource."""
    labels = {
        LABEL_GROUP: group,
        LABEL_MANAGED_BY: MANAGED_BY_VALUE,
        LABEL_OWNER: _sanitize(owner),
    }
    if offering:
        labels[LABEL_OFFERING] = offering
    return labels


def workload_labels(
    group: str, owner: str, workload: str, offering: str | None = None
) -> dict[str, str]:
    """Ownership labels plus the workload (function/container) name."""
    labels = ownership_labels(group, owner, offering)
    labels[LABEL_WORKLOAD] = workload
    return labels


def _sanitize(value: str) -> str:
    """Make an arbitrary identifier safe for a label value (<=63 chars)."""
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in value)
    return safe[:63].strip("-_.") or "unknown"
