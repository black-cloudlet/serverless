"""Pure builders for API-managed Secret / ConfigMap manifests (docs §7.3)."""

from __future__ import annotations

import base64

from app.services.labels import ownership_labels


def build_configmap(name: str, group: str, owner: str, data: dict[str, str]) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "labels": ownership_labels(group, owner)},
        "data": dict(data),
    }


def build_secret(
    name: str,
    group: str,
    owner: str,
    data: dict[str, str],
    secret_type: str = "Opaque",
) -> dict:
    encoded = {
        k: base64.b64encode(v.encode("utf-8")).decode("ascii") for k, v in data.items()
    }
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": secret_type,
        "metadata": {"name": name, "labels": ownership_labels(group, owner)},
        "data": encoded,
    }
