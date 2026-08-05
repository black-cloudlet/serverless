"""Pure builders/decoders for the credential Secrets a workload owns."""

from __future__ import annotations

import base64
import json
from urllib.parse import urlsplit

from api.services.manifests import resources as res

# Data keys of the ``{workload}-git`` basic-auth Secret.
GIT_USERNAME_KEY = "username"
GIT_TOKEN_KEY = "password"  # noqa: S105 - a Secret data key name, not a credential

# kpack matches a credential to a repository through this annotation.
GIT_ANNOTATION = "kpack.io/git"


def registry_of(image: str) -> str:
    """The registry host an image reference points at, used to key its pull secret."""
    first = image.split("/", 1)[0]
    if "/" in image and ("." in first or ":" in first or first == "localhost"):
        return first
    return "docker.io"


def pull_secret_name(workload: str) -> str:
    """The name of a workload's image-pull Secret: ``{workload}-pull``."""
    return f"{workload}-pull"


def build_pull_secret(
    name: str,
    labels: dict[str, str],
    registry: str,
    username: str,
    token: str,
) -> dict:
    """Build a dockerconfigjson image-pull Secret for one registry."""
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

    Shown on read (like a secret's name); the token is never returned to clients.
    """
    return _registry_field(secret, "username")


def registry_token(secret: dict) -> str | None:
    """Decode the registry password/token from a dockerconfigjson Secret."""
    return _registry_field(secret, "password")


def _registry_field(secret: dict, field: str) -> str | None:
    """Decode one field ("username"/"password") from a dockerconfigjson Secret."""
    raw = (secret.get("data") or {}).get(".dockerconfigjson")
    if not raw:
        return None
    try:
        auths = json.loads(base64.b64decode(raw)).get("auths") or {}
        for entry in auths.values():
            if entry.get(field):
                return entry[field]
    except Exception:  # noqa: BLE001 - malformed secret -> treat as unknown
        return None
    return None


def git_secret_name(workload: str) -> str:
    """The name of a workload's git-token Secret: ``{workload}-git``."""
    return f"{workload}-git"


def build_git_secret(
    name: str,
    labels: dict[str, str],
    token: str,
    git_url: str = "",
    username: str = "x-access-token",
) -> dict:
    """Build the ``kubernetes.io/basic-auth`` Secret holding a function's git token."""
    secret = res.build_secret(
        name,
        labels,
        {GIT_USERNAME_KEY: username, GIT_TOKEN_KEY: token},
        "kubernetes.io/basic-auth",
    )
    if git_url:
        secret["metadata"]["annotations"] = {GIT_ANNOTATION: git_credential_host(git_url)}
    return secret


def git_credential_host(git_url: str) -> str:
    """The ``kpack.io/git`` annotation value for a repository URL.

    kpack compares this annotation against the repository URL to pick a
    credential, so it must be scheme and host only - no path, no userinfo.
    """
    parts = urlsplit(git_url)
    if not parts.scheme or not parts.netloc:
        return git_url
    return f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}"
