"""Pure builder for an imagePullSecret from customer registry credentials.

The token is used transiently to materialize the pull secret; it is never
persisted by the API itself (docs §7.2).
"""

from __future__ import annotations

import base64
import json

from app.services.labels import ownership_labels


def build_pull_secret(
    name: str,
    group: str,
    owner: str,
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
        "metadata": {"name": name, "labels": ownership_labels(group, owner)},
        "data": {".dockerconfigjson": encoded},
    }
