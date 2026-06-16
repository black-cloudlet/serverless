"""Helpers for the ownership labels stamped on every managed resource."""

from __future__ import annotations

from app.models.common import (
    LABEL_GROUP,
    LABEL_MANAGED_BY,
    LABEL_OFFERING,
    LABEL_OWNER,
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


def group_selector(groups: list[str]) -> str:
    """Label selector restricting a list/get to the caller's group(s)."""
    return f"{LABEL_GROUP} in ({','.join(groups)})"


def _sanitize(value: str) -> str:
    """Make an arbitrary identifier safe for a label value (<=63 chars)."""
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in value)
    return safe[:63].strip("-_.") or "unknown"
