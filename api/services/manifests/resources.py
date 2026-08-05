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
    """Build a ConfigMap manifest, routing non-text values to ``binaryData``."""
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
    """Build a Secret manifest, base64-encoding the data values."""
    encoded = {k: base64.b64encode(_as_bytes(v)).decode("ascii") for k, v in data.items()}
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": secret_type,
        "metadata": {"name": name, "labels": dict(labels)},
        "data": encoded,
    }


def owner_reference(owner: dict) -> dict | None:
    """Build an ownerReference pointing at an already-applied object."""
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
    """
    if not owner_ref:
        return manifest
    out = dict(manifest)
    out["metadata"] = {**(out.get("metadata") or {}), "ownerReferences": [owner_ref]}
    return out
