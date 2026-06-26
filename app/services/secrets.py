"""Pure builder for an imagePullSecret from customer registry credentials.

The token is used transiently to materialize the pull secret; it is never
persisted by the API itself (docs §7.2). The caller supplies the labels.
"""

from __future__ import annotations

import base64
import json


def registry_of(image: str) -> str:
    """The registry host an image reference points at, used to key its pull secret.

    The org runs several registries, so this must come from the client's image,
    not our platform registry. Falls back to Docker Hub when the reference carries
    no explicit registry (e.g. ``nginx:latest`` or ``team/app:tag``). The registry
    is the first path segment only when it looks like a host (has a ``.`` or
    ``:port``, or is ``localhost``); otherwise it's an implicit Docker Hub
    namespace.

    Args:
        image: The image reference (e.g. ``reg.example.com/team/app:tag``).

    Returns:
        The registry host, or ``"docker.io"`` when none is explicit.
    """
    first = image.split("/", 1)[0]
    if "/" in image and ("." in first or ":" in first or first == "localhost"):
        return first
    return "docker.io"


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


def registry_username(secret: dict) -> str | None:
    """Decode the registry username from a dockerconfigjson Secret.

    The password (token) is deliberately never returned.

    Returns:
        The username, or None if it can't be read.
    """
    raw = (secret.get("data") or {}).get(".dockerconfigjson")
    if not raw:
        return None
    try:
        auths = json.loads(base64.b64decode(raw)).get("auths") or {}
        for entry in auths.values():
            if entry.get("username"):
                return entry["username"]
    except Exception:  # noqa: BLE001 - malformed secret -> treat as unknown
        return None
    return None
