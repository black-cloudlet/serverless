"""Pure builders for Secret / ConfigMap manifests.

The caller supplies the labels (see ``services.labels``) so each builder stays
agnostic about ownership/workload labelling.
"""

from __future__ import annotations

import base64


def build_configmap(name: str, labels: dict[str, str], data: dict[str, str]) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "labels": dict(labels)},
        "data": dict(data),
    }


def build_secret(
    name: str,
    labels: dict[str, str],
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
        "metadata": {"name": name, "labels": dict(labels)},
        "data": encoded,
    }
