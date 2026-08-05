"""The kpack vocabulary: manifests for a build, and how to read its status.

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
    """Build the per-function build ServiceAccount."""
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
    """Build the kpack ``Image`` CR for one function."""
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
    """
    return {"metadata": {"annotations": {BUILD_TRIGGER_ANNOTATION: at}}}


def build_status(image: dict | None) -> tuple[str, str | None, str | None]:
    """Reduce an ``Image``'s status to ``(state, latest_image, message)``."""
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
