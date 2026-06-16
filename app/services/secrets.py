"""Pure builder for an imagePullSecret from customer registry credentials.

The token is used transiently to materialize the pull secret; it is never
persisted by the API itself (docs §7.2). The caller supplies the labels.
"""

from __future__ import annotations

import base64
import json


def build_pull_secret(
    name: str,
    labels: dict[str, str],
    registry: str,
    username: str,
    token: str,
) -> dict:
    auth = base64.b64encode(f"{username}:{token}".encode()).decode("ascii")
    dockercfg = {"auths": {registry: {"username": username, "password": token, "auth": auth}}}
    encoded = base64.b64encode(json.dumps(dockercfg).encode()).decode("ascii")
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "kubernetes.io/dockerconfigjson",
        "metadata": {"name": name, "labels": dict(labels)},
        "data": {".dockerconfigjson": encoded},
    }
