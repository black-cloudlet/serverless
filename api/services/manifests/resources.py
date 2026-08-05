"""Pure builders for Secret / ConfigMap manifests.

The caller supplies the labels (see ``services.labels``) so each builder stays
agnostic about ownership/workload labelling.
"""

from __future__ import annotations

import base64


def _as_bytes(value: str | bytes) -> bytes:
    """The value as raw bytes; text is encoded UTF-8, bytes pass through."""
    return value if isinstance(value, bytes) else value.encode("utf-8")


def build_configmap(name: str, labels: dict[str, str], data: dict[str, str | bytes]) -> dict:
    """Build a ConfigMap manifest, routing non-text values to ``binaryData``.

    A ConfigMap's ``data`` holds UTF-8 text only; anything else belongs in
    ``binaryData`` as base64. Both mount identically, so the caller need not care
    which a given file lands in - but writing a non-UTF-8 byte into ``data`` would
    be rejected by the API server (or corrupt the file), so the split is made here
    rather than left to the caller.

    Args:
        name: The ConfigMap name.
        labels: Labels to stamp on it.
        data: The key/value data; ``bytes`` (or text that is not valid UTF-8)
            goes to ``binaryData``.

    Returns:
        The ConfigMap manifest dict.
    """
    text: dict[str, str] = {}
    binary: dict[str, str] = {}
    for key, value in data.items():
        raw = _as_bytes(value)
        try:
            text[key] = raw.decode("utf-8")
        except UnicodeDecodeError:
            binary[key] = base64.b64encode(raw).decode("ascii")
    manifest: dict = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "labels": dict(labels)},
        "data": text,
    }
    if binary:
        manifest["binaryData"] = binary
    return manifest


def build_secret(
    name: str,
    labels: dict[str, str],
    data: dict[str, str | bytes],
    secret_type: str = "Opaque",  # noqa: S107 - the k8s Secret `type` field, not a password
) -> dict:
    """Build a Secret manifest, base64-encoding the data values.

    Values may be ``bytes``: a Secret's ``data`` is base64 either way, so binary
    material (a keystore, a DER certificate) needs no separate field the way a
    ConfigMap does. Text is encoded UTF-8 first.

    Args:
        name: The Secret name.
        labels: Labels to stamp on it.
        data: The plaintext key/value data (encoded into ``data``).
        secret_type: The Kubernetes Secret type.

    Returns:
        The Secret manifest dict.
    """
    encoded = {k: base64.b64encode(_as_bytes(v)).decode("ascii") for k, v in data.items()}
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": secret_type,
        "metadata": {"name": name, "labels": dict(labels)},
        "data": encoded,
    }


def owner_reference(owner: dict) -> dict | None:
    """Build an ownerReference pointing at an already-applied object.

    The owner's live ``metadata.uid`` is required. A resource carrying this
    reference is garbage-collected by Kubernetes when the owner is deleted.
    ``blockOwnerDeletion``/``controller`` are left ``false`` so the API needs no
    extra permission on the owner's finalizers; cascade is ordinary background GC.

    Args:
        owner: The already-applied owner object (must include ``metadata.uid``).

    Returns:
        The ownerReference dict, or None if the owner has no uid yet.
    """
    meta = owner.get("metadata", {}) or {}
    uid = meta.get("uid")
    if not uid:
        return None
    return {
        "apiVersion": owner.get("apiVersion"),
        "kind": owner.get("kind"),
        "name": meta.get("name"),
        "uid": uid,
        "controller": False,
        "blockOwnerDeletion": False,
    }


def with_owner(manifest: dict, owner_ref: dict | None) -> dict:
    """Return a shallow copy of ``manifest`` with ``owner_ref`` set as its owner.

    A no-op returning the original if ``owner_ref`` is None. Does not mutate the
    input.

    Args:
        manifest: The manifest to own.
        owner_ref: The ownerReference to set, or None.

    Returns:
        The manifest (copy) carrying the owner reference, or the original.
    """
    if not owner_ref:
        return manifest
    out = dict(manifest)
    out["metadata"] = {**(out.get("metadata") or {}), "ownerReferences": [owner_ref]}
    return out
