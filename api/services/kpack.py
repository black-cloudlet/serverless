"""Pure builders for the kpack objects a function build needs.

Three objects per function, all in the build namespace (docs §2.1): a git Secret
in the shape kpack consumes, the ServiceAccount pairing it with the shared
registry credential, and the ``Image`` CR itself. No I/O here - the caller
applies them (see :mod:`api.services.builder`).

Names are prefixed ``fn-`` so they never collide with the workloads-namespace
objects of the same function, in particular the API's own ``{workload}-git``
Secret, which is a different shape in a different namespace.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from api.services import resources as res

GIT_ANNOTATION = "kpack.io/git"


def build_object_name(workload: str) -> str:
    """Name shared by a function's Image and build ServiceAccount: ``fn-{workload}``."""
    return f"fn-{workload}"


def git_secret_name(workload: str) -> str:
    """Name of a function's kpack git Secret: ``fn-{workload}-git``."""
    return f"fn-{workload}-git"


def git_credential_host(git_url: str) -> str:
    """The ``kpack.io/git`` annotation value for a repository URL.

    kpack matches a credential to a source repository by comparing this
    annotation against the repository URL, so it must be the scheme and host
    with no path and no embedded userinfo.

    Args:
        git_url: The repository URL (e.g. ``https://git.internal/team/app.git``).

    Returns:
        ``{scheme}://{host}`` (e.g. ``https://git.internal``); the input
        unchanged if it has no scheme to split on.
    """
    parts = urlsplit(git_url)
    if not parts.scheme or not parts.netloc:
        return git_url
    host = parts.netloc.rsplit("@", 1)[-1]  # drop any user:pass@
    return f"{parts.scheme}://{host}"


def build_git_secret(
    name: str, labels: dict[str, str], git_url: str, username: str, token: str
) -> dict:
    """Build the ``kubernetes.io/basic-auth`` git Secret kpack clones with.

    Distinct from the API's own ``{workload}-git`` Secret, which is Opaque with a
    single ``token`` key: kpack only reads basic-auth (or ssh-auth) Secrets, and
    only when they carry the ``kpack.io/git`` annotation naming the repository
    host. Both hold the same caller-supplied token; the Opaque one is the API's
    durable copy (readable, in the workloads namespace) and this one is kpack's
    (write-only, in the build namespace). docs/BUILD-PIPELINE.md §6.

    Args:
        name: The Secret name.
        labels: Labels to stamp on it.
        git_url: The repository URL, reduced to scheme+host for the annotation.
        username: Username paired with the token (``build.git_username``).
        token: The caller-supplied git token.

    Returns:
        The Secret manifest dict.
    """
    secret = res.build_secret(
        name, labels, {"username": username, "password": token}, "kubernetes.io/basic-auth"
    )
    secret["metadata"]["annotations"] = {GIT_ANNOTATION: git_credential_host(git_url)}
    return secret


def build_service_account(
    name: str, labels: dict[str, str], git_secret: str, registry_secret: str
) -> dict:
    """Build the per-function build ServiceAccount.

    Per function rather than shared because each function's git token comes from
    its own caller: a shared account would let one tenant's build authenticate
    as another. The registry credential is platform-wide and appears in both
    lists - ``secrets`` is what kpack pushes the built image with, and
    ``imagePullSecrets`` is what the build pod pulls the builder image with.

    Args:
        name: The ServiceAccount name.
        labels: Labels to stamp on it.
        git_secret: The function's basic-auth git Secret.
        registry_secret: The shared registry dockerconfigjson Secret.

    Returns:
        The ServiceAccount manifest dict.
    """
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": name, "labels": dict(labels)},
        "secrets": [{"name": git_secret}, {"name": registry_secret}],
        "imagePullSecrets": [{"name": registry_secret}],
    }


def build_image(
    name: str,
    labels: dict[str, str],
    *,
    tag: str,
    builder: str,
    service_account: str,
    git_url: str,
    revision: str,
    env: list[dict[str, str]] | None = None,
    resources: dict | None = None,
) -> dict:
    """Build the kpack ``Image`` CR for one function.

    Applied with server-side apply on every create and update, which is what
    makes it safe under active/active: the spec is a pure function of the
    request, so two instances applying concurrently converge instead of
    fighting, and a site that has never seen the function reconstructs it on the
    next write. ``creationTime`` is deliberately never set - it is a nonce and
    would make every apply look like a change and trigger an endless rebuild.

    Args:
        name: The Image name (``fn-{workload}``).
        labels: Labels to stamp on it.
        tag: Where the built image is pushed.
        builder: Name of the namespaced Builder to build with.
        service_account: The per-function build ServiceAccount.
        git_url: Source repository URL.
        revision: Branch or commit SHA to build.
        env: Build-time environment (runtime version, package index URLs, the
            dependency mirror).
        resources: Requests/limits for the build pod.

    Returns:
        The Image manifest dict.
    """
    spec: dict = {
        "tag": tag,
        # Namespace omitted: kpack resolves a namespaced Builder in the Image's
        # own namespace, which is where the chart puts them.
        "builder": {"kind": "Builder", "name": builder},
        "serviceAccountName": service_account,
        "source": {"git": {"url": git_url, "revision": revision}},
    }
    build: dict = {}
    if env:
        build["env"] = [dict(e) for e in env]
    if resources:
        build["resources"] = dict(resources)
    if build:
        spec["build"] = build
    return {
        "apiVersion": "kpack.io/v1alpha2",
        "kind": "Image",
        "metadata": {"name": name, "labels": dict(labels)},
        "spec": spec,
    }


def build_status(image: dict | None) -> tuple[str, str | None, str | None]:
    """Reduce an ``Image``'s status to ``(state, latest_image, message)``.

    kpack reports progress through the standard ``Ready`` condition on the
    Image: ``True`` once the latest build succeeded, ``False`` when it failed,
    and ``Unknown`` while a build is running. ``latestImage`` is the digest of
    the last *successful* build, so it can be present while a newer build is in
    flight or has failed - the state, not the presence of an image, is what says
    whether the function is current.

    Args:
        image: The Image object, or None when there is none.

    Returns:
        ``state`` is one of ``Building``/``Ready``/``Failed``, or ``Unknown``
        only when there is no Image at all - an Image that exists but has not
        been reconciled yet is ``Building``, not ``Unknown``;
        ``latest_image`` is the last successful digest when known; ``message``
        is the condition message on failure.
    """
    if image is None:
        return "Unknown", None, None
    status = image.get("status") or {}
    latest = status.get("latestImage")
    ready = next(
        (c for c in (status.get("conditions") or []) if c.get("type") == "Ready"),
        None,
    )
    if ready is None:
        # Applied but not yet reconciled - no conditions written at all.
        return "Building", latest, None
    state = {"True": "Ready", "False": "Failed"}.get(str(ready.get("status")), "Building")
    return state, latest, ready.get("message")
