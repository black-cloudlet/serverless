"""Rewriting a read-back Knative Service to run a newly built digest.

The API owns the KSVC spec and the controller owns one field of it, so these
helpers edit the live object read back from the cluster instead of composing
one. Pure dict work (docs/BUILDING.md - What it writes).
"""

from __future__ import annotations

import copy

from common.labels import LABEL_OFFERING, OFFERING_FUNCTION

# Comes back on a read; `managedFields` is rejected outright on the way in.
_SERVER_METADATA = (
    "creationTimestamp",
    "generation",
    "managedFields",
    "resourceVersion",
    "selfLink",
    "uid",
)
_CLIENT_APPLY_ANNOTATION = "kubectl.kubernetes.io/last-applied-configuration"


def _containers(ksvc: dict) -> list[dict]:
    """The pod template's container list, or an empty list if it has none."""
    template = (ksvc.get("spec") or {}).get("template") or {}
    return (template.get("spec") or {}).get("containers") or []


def deployed_image(ksvc: dict) -> str | None:
    """The first container's image, the only one this platform writes.

    Args:
        ksvc: The Knative Service object.

    Returns:
        The image reference, or None.
    """
    containers = _containers(ksvc)
    return str(containers[0]["image"]) if containers and containers[0].get("image") else None


def needs_image(ksvc: dict, image: str) -> bool:
    """Whether ``image`` should replace what this KSVC currently runs.

    False when the KSVC already runs that reference, and when it is not labelled
    as a function. Only the whole reference is compared, so a build that moved to
    another repository still rolls out (docs/BUILDING.md - What it writes).

    Args:
        ksvc: The live Knative Service object.
        image: The digest reference the build pushed.

    Returns:
        True if the KSVC should be applied with ``image``.
    """
    labels = (ksvc.get("metadata") or {}).get("labels") or {}
    if labels.get(LABEL_OFFERING) != OFFERING_FUNCTION:
        return False
    current = deployed_image(ksvc)
    return bool(current) and current != image


def with_image(ksvc: dict, image: str) -> dict:
    """A copy of a read-back KSVC, running ``image`` and safe to re-apply.

    Args:
        ksvc: The live Knative Service object.
        image: The image reference to run.

    Returns:
        A new manifest; the input is not mutated.
    """
    out = copy.deepcopy(ksvc)
    out.pop("status", None)

    meta = out.get("metadata") or {}
    for key in _SERVER_METADATA:
        meta.pop(key, None)
    annotations = meta.get("annotations") or {}
    annotations.pop(_CLIENT_APPLY_ANNOTATION, None)

    template = (out.get("spec") or {}).get("template") or {}
    # Knative rejects a template whose name is unchanged while its content is not.
    (template.get("metadata") or {}).pop("name", None)
    containers = _containers(out)
    if containers:
        containers[0]["image"] = image
    return out
