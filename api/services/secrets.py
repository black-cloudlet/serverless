"""Pure builders/decoders for the credential Secrets a workload owns.

Covers the imagePullSecret built from customer registry credentials and the
Opaque ``{workload}-git`` Secret that stores a function's git token so it
survives edits and can be reused to rebuild without the client re-sending it.
Secret *values* are never returned on read - only the workload's update path
reads them back, to preserve a "keep" (redacted) field. The caller supplies the
labels.
"""

from __future__ import annotations

import base64
import json

from api.services import resources as res

# Data key of the git token inside the ``{workload}-git`` Secret.
GIT_TOKEN_KEY = "token"  # noqa: S105 - a Secret data key name, not a credential


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
    """Build a dockerconfigjson image-pull Secret for one registry.

    Args:
        name: The Secret name.
        labels: Labels to stamp on it.
        registry: The registry host the credentials are scoped to.
        username: The registry username.
        token: The registry password/token.

    Returns:
        The pull-secret manifest dict.
    """
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

    Args:
        secret: The dockerconfigjson Secret object.

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


def git_secret_name(workload: str) -> str:
    """The name of a workload's git-token Secret: ``{workload}-git``."""
    return f"{workload}-git"


def build_git_secret(name: str, labels: dict[str, str], token: str) -> dict:
    """Build an Opaque Secret holding a function's git token.

    Stored so the token survives edits and can be reused to rebuild without the
    client re-supplying it. The value is never returned on read.

    Args:
        name: The Secret name (``{workload}-git``).
        labels: Labels to stamp on it.
        token: The git token to store.

    Returns:
        The Secret manifest dict.
    """
    return res.build_secret(name, labels, {GIT_TOKEN_KEY: token})


def git_token(secret: dict) -> str | None:
    """Decode the git token from a ``{workload}-git`` Secret (update path only).

    Args:
        secret: The Opaque git Secret object.

    Returns:
        The token, or None if it can't be read.
    """
    raw = (secret.get("data") or {}).get(GIT_TOKEN_KEY)
    if not raw:
        return None
    try:
        return base64.b64decode(raw).decode("utf-8", "surrogateescape")
    except Exception:  # noqa: BLE001 - malformed secret -> treat as unknown
        return None
