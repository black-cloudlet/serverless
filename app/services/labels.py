"""Helpers for the ownership labels stamped on every managed resource."""

from __future__ import annotations

from app.models.common import (
    LABEL_GROUP,
    LABEL_MANAGED_BY,
    LABEL_OFFERING,
    LABEL_OWNER,
    LABEL_WORKLOAD,
    MANAGED_BY_VALUE,
)


def ownership_labels(
    group: str, owner: str, offering: str | None = None
) -> dict[str, str]:
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
    """
    labels = ownership_labels(group, owner, offering)
    labels[LABEL_WORKLOAD] = workload
    return labels


def _sanitize(value: str) -> str:
    """Make an arbitrary identifier safe for a label value (<=63 chars)."""
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in value)
    return safe[:63].strip("-_.") or "unknown"
