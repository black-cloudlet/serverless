"""The kpack vocabulary: manifests for a build, and how to read its status.

Shared rather than API-local because both halves of the build path speak it -
the API writes the ``Image`` (:mod:`api.services.builder`), and the build
service reads ``status.latestImage`` back off it (docs/BUILD-PIPELINE.md §8).
Naming it in one place is what keeps them agreeing on which object is which.

Pure: no I/O and no framework, so it sits in the domain layer beside
:mod:`common.contract`. The caller applies the manifests alongside the KSVC's
other derived resources, in the workload's own namespace, so they are
owner-stamped and garbage-collected with it.

The git credential is not here - it is the workload's own ``{workload}-git``
Secret, which :mod:`api.services.secrets` builds in the shape kpack consumes.
"""

from __future__ import annotations


def build_object_name(workload: str) -> str:
    """Name shared by a function's Image and build ServiceAccount: ``fn-{workload}``.

    Prefixed so it cannot collide with the KSVC or any other object the workload
    owns in the same namespace.
    """
    return f"fn-{workload}"


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
        git_secret: The workload's basic-auth git Secret.
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
