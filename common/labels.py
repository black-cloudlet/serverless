"""Ownership labels stamped on every managed resource (shared by all services).

The label *keys* and the ``managed-by`` value are the platform's identity
convention: the API stamps them on every workload and on everything derived from
it - config Secrets, the DomainMapping, the kpack build objects - so anything the
platform creates is attributable and selectable the same way.
"""

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
    """Build the ownership/management labels stamped on every resource.

    Args:
        group: The owning group.
        owner: The creating username.
        offering: The offering ("function"/"container"), when applicable.

    Returns:
        The label dict (group, managed-by, owner, and offering when given).
    """
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
    """Ownership labels plus the workload (function/container) name.

    Every resource the API creates for a function/container carries both the SSO
    group and the workload name so it is unambiguously attributable and
    selectable.

    Args:
        group: The owning group.
        owner: The creating username.
        workload: The object name (``{name}-{group}``).
        offering: The offering, when applicable.

    Returns:
        The label dict including the workload label.
    """
    labels = ownership_labels(group, owner, offering)
    labels[LABEL_WORKLOAD] = workload
    return labels


def _sanitize(value: str) -> str:
    """Make an arbitrary identifier safe for a label value (<=63 chars)."""
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in value)
    return safe[:63].strip("-_.") or "unknown"
