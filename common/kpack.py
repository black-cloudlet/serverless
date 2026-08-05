"""The kpack vocabulary: manifests for a build, and how to read its status.

Shared rather than API-local because both halves of the build path speak it -
the API writes the ``Image`` (:mod:`api.services.builder.kpack_backend`), and the build
service reads ``status.latestImage`` back off it (docs/BUILDING.md - Ownership).
Naming it in one place is what keeps them agreeing on which object is which.

Pure: no I/O and no framework, so it sits in the domain layer beside
:mod:`common.build`. The caller applies the manifests alongside the KSVC's
other derived resources, in the workload's own namespace, so they are
owner-stamped and garbage-collected with it.

The git credential is not here - it is the workload's own ``{workload}-git``
Secret, which :mod:`api.services.manifests.secrets` builds in the shape kpack consumes.
"""

from __future__ import annotations

# Read by kpack off the latest Build, never the Image (docs/BUILDING.md - What
# causes a new Build).
BUILD_TRIGGER_ANNOTATION = "image.kpack.io/additionalBuildNeeded"
IMAGE_LABEL = "image.kpack.io/image"
BUILD_NUMBER_LABEL = "image.kpack.io/buildNumber"


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

    Per function, not shared: each function's git token comes from its own caller, so
    a shared account would let one tenant's build authenticate as another. The
    registry credential is platform-wide and appears in both lists - ``secrets`` to
    push the built image, ``imagePullSecrets`` to pull the builder.

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
    sub_path: str = "",
    env: list[dict[str, str]] | None = None,
    resources: dict | None = None,
    cache_tag: str | None = None,
    success_history_limit: int | None = None,
    failed_history_limit: int | None = None,
) -> dict:
    """Build the kpack ``Image`` CR for one function.

    Server-side applied on every create and update, which is what makes it safe under
    active/active: the spec is a pure function of the request, so concurrent applies
    converge. ``creationTime`` is never set - it is a nonce that would make every
    apply look like a change and rebuild forever.

    Args:
        name: The Image name (``fn-{workload}``).
        labels: Labels to stamp on it.
        tag: Where the built image is pushed.
        builder: Name of the namespaced Builder to build with.
        service_account: The per-function build ServiceAccount.
        git_url: Source repository URL.
        revision: Branch or commit SHA to build.
        sub_path: Directory within the clone to build; "" builds the root.
        env: Build-time environment (runtime version, package index URLs, the
            dependency mirror).
        resources: Requests/limits for the build pod.
        cache_tag: Registry reference to cache build layers in. None omits
            ``spec.cache``, which is not "no cache" - it takes the kpack
            install's default, a PVC per Image.
        success_history_limit: Successful Builds kpack keeps for this function.
        failed_history_limit: Failed Builds kpack keeps. None omits the fields,
            which is kpack's default of 10 each, not "unbounded"
            (docs/BUILDING.md - Build history).

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
    # subPath sits on source, beside git - it selects the directory the
    # buildpacks detect and build in, not the ref that is cloned.
    if sub_path:
        spec["source"]["subPath"] = sub_path
    # The build ServiceAccount already pushes to this registry, so the cache
    # needs no credential of its own.
    if cache_tag:
        spec["cache"] = {"registry": {"tag": cache_tag}}
    if success_history_limit is not None:
        spec["successBuildHistoryLimit"] = success_history_limit
    if failed_history_limit is not None:
        spec["failedBuildHistoryLimit"] = failed_history_limit
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


def _build_number(build: dict) -> int | None:
    """The build number kpack labelled one ``Build`` with, or None if it has none."""
    raw = ((build.get("metadata") or {}).get("labels") or {}).get(BUILD_NUMBER_LABEL)
    if not isinstance(raw, str) or not raw.isdigit():
        return None
    return int(raw)


def latest_build(builds: list[dict]) -> dict | None:
    """The highest-numbered ``Build`` of one ``Image``, or None if it has none.

    Ordered on the build number, not the creation timestamp, which has only
    one-second resolution.

    Args:
        builds: The Builds selected by :data:`IMAGE_LABEL` for one Image.

    Returns:
        The latest Build, or None.
    """
    numbered = [(_build_number(b), b) for b in builds]
    ordered = [(number, build) for number, build in numbered if number is not None]
    if not ordered:
        return None
    return max(ordered, key=lambda pair: pair[0])[1]


def trigger_patch(at: str) -> dict:
    """The merge patch asking kpack for one more build, for the latest ``Build``.

    kpack tests only for the annotation's presence, so the value is free to be
    the timestamp that explains why the next build exists.

    Args:
        at: When the rebuild was asked for.

    Returns:
        The patch body.
    """
    return {"metadata": {"annotations": {BUILD_TRIGGER_ANNOTATION: at}}}


def build_status(image: dict | None) -> tuple[str, str | None, str | None]:
    """Reduce an ``Image``'s status to ``(state, latest_image, message)``.

    kpack reports progress on the ``Ready`` condition: ``True`` once the latest build
    succeeded, ``False`` on failure, ``Unknown`` while running. ``latestImage`` is the
    last *successful* digest, so it can be set while a newer build fails - the state,
    not the image, says whether the function is current.

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
