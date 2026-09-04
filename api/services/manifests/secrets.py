"""Pure builders/decoders for the credential Secrets a workload owns.

Covers the imagePullSecret built from customer registry credentials, the
``{workload}-git`` Secret holding a function's token so a later edit can rebuild
without it being re-sent, and the ``{workload}-webhook`` Secret holding the token
a git webhook authenticates a push with. Customer-supplied values are never
returned on read - only the update path reads them back, to preserve a redacted
"keep" field. The webhook token is the exception, and it is the platform's own:
it exists to be pasted into GitLab, so it is returned (docs/FUNCTIONS.md - Git
webhook).
"""

from __future__ import annotations

import base64
import json
import secrets
from urllib.parse import urlsplit

from api.services.manifests import resources as res

# Data keys of the ``{workload}-git`` basic-auth Secret.
GIT_USERNAME_KEY = "username"
GIT_TOKEN_KEY = "password"  # noqa: S105 - a Secret data key name, not a credential

# kpack matches a credential to a repository through this annotation.
GIT_ANNOTATION = "kpack.io/git"

# Data key of the ``{workload}-webhook`` Secret.
WEBHOOK_TOKEN_KEY = "token"  # noqa: S105 - a Secret data key name, not a credential

# Bytes of entropy in a generated webhook token. 32 bytes is 256 bits, which
# `token_urlsafe` renders as 43 URL-safe characters - short enough for GitLab's
# field, long enough that guessing is not a threat model.
WEBHOOK_TOKEN_BYTES = 32


def registry_of(image: str) -> str:
    """The registry host an image reference points at, used to key its pull secret.

    The host is read off the caller's own image reference. The first path segment
    counts as a host only when it contains a ``.`` or a ``:`` or is ``localhost``;
    otherwise the reference names no registry and this falls back to Docker Hub.

    Args:
        image: The image reference (e.g. ``reg.example.com/team/app:tag``).

    Returns:
        The registry host, or ``"docker.io"`` when none is explicit.
    """
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

    Shown on read (like a secret's name); the token is never returned to clients.

    Args:
        secret: The dockerconfigjson Secret object.

    Returns:
        The username, or None if it can't be read.
    """
    return _registry_field(secret, "username")


def registry_token(secret: dict) -> str | None:
    """Decode the registry password/token from a dockerconfigjson Secret.

    Internal use only (never returned to clients): the update path reads it back to
    re-materialize the pull secret against the current image's registry when the
    caller keeps the credential (token omitted).

    Args:
        secret: The dockerconfigjson Secret object.

    Returns:
        The password/token, or None if it can't be read.
    """
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
    """Build the ``kubernetes.io/basic-auth`` Secret holding a function's git token.

    One Secret serves both readers: the API reads it back to rebuild without the
    token being re-supplied, and kpack clones with it. It is ``basic-auth``
    annotated ``kpack.io/git`` because that is the only shape kpack reads, and one
    object suffices because builds run in the workload's own namespace
    (docs/BUILDING.md - Git credential - per function, never shared).

    Args:
        name: The Secret name (``{workload}-git``).
        labels: Labels to stamp on it.
        token: The git token to store, as the basic-auth password.
        git_url: Source repository, reduced to scheme+host for the annotation.
            Omitted for a token stored outside a build context.
        username: Username paired with the token. GitHub and GitLab PATs accept
            any value; providers that check it need the real one.

    Returns:
        The Secret manifest dict.
    """
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

    Args:
        git_url: The repository URL.

    Returns:
        ``{scheme}://{host}``; the input unchanged if it has no scheme to split.
    """
    parts = urlsplit(git_url)
    if not parts.scheme or not parts.netloc:
        return git_url
    return f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}"


def webhook_secret_name(workload: str) -> str:
    """The name of a function's webhook-token Secret: ``{workload}-webhook``."""
    return f"{workload}-webhook"


def new_webhook_token() -> str:
    """Mint a webhook token.

    Returns:
        A fresh URL-safe token of :data:`WEBHOOK_TOKEN_BYTES` bytes of entropy.
    """
    return secrets.token_urlsafe(WEBHOOK_TOKEN_BYTES)


def build_webhook_secret(name: str, labels: dict[str, str], token: str) -> dict:
    """Build the Secret holding a function's webhook token.

    A Secret rather than an annotation on the KSVC: this token is what lets an
    *unauthenticated* caller start a build, and an annotation is readable by
    anyone with ``get`` on the workload and is copied into every event and log
    line that prints the object. It is replicated to every region like the git
    Secret, so a push still authenticates after a switchover, when the API
    answering is the other region's (docs/BUILDING.md - Active/Active Behaviour).

    Args:
        name: The Secret name (``{workload}-webhook``).
        labels: Labels to stamp on it.
        token: The webhook token to store.

    Returns:
        The Secret manifest dict.
    """
    return res.build_secret(name, labels, {WEBHOOK_TOKEN_KEY: token})
