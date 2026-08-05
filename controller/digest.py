"""Rewriting a read-back Knative Service to run a newly built digest.

Pure - no cluster call, no framework - so the rules that decide *whether* a
digest belongs on a workload are testable on plain dicts.

The controller does not compose a KSVC. The API owns that spec; the controller
owns one field of it, and everything else must survive the round trip untouched.
So the object it applies is the live one, edited - which is why most of this
module is about making a read-back object safe to send back.
"""

from __future__ import annotations

import copy

from common.labels import LABEL_OFFERING, OFFERING_FUNCTION
from common.names import repository_of

# Server-owned metadata that comes back on a read. `managedFields` is the one
# the API server rejects outright; the rest would just be asserted back as if
# they were desired state.
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
    """The image the workload runs, or None if unreadable.

    The first container, which for anything this platform writes is the only
    one (:func:`api.services.manifests.ksvc.build_ksvc`).

    Args:
        ksvc: The Knative Service object.

    Returns:
        The image reference, or None.
    """
    containers = _containers(ksvc)
    return str(containers[0]["image"]) if containers and containers[0].get("image") else None


def needs_image(ksvc: dict, image: str) -> bool:
    """Whether ``image`` should replace what this KSVC currently runs.

    Three ways the answer is no, and each is a different mistake to avoid:

    - it already runs it - the loop's normal outcome, and the reason a resync
      costs nothing;
    - it is not a function - an Image's digest has no business on a container
      offering that happens to have reused a deleted function's name;
    - it is a different repository - the KSVC's reference is desired state the
      API writes, and only the digest half is the controller's to supply. After
      a registry-layout change (docs/BUILDING.md - Registry layout) an `Image`
      left under the old layout would otherwise pull the workload backwards.

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
    if not current or current == image:
        return False
    return repository_of(current) == repository_of(image)


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
    # A pinned revision name would make this apply fail: Knative rejects a
    # template whose name is unchanged while its content is not. Nothing the API
    # writes sets one, so dropping it costs nothing and removes the one way a
    # hand-edited KSVC could wedge the loop.
    (template.get("metadata") or {}).pop("name", None)
    containers = _containers(out)
    if containers:
        containers[0]["image"] = image
    return out
