"""Pure builder for a Knative Service (KSVC) manifest."""

from __future__ import annotations

from dataclasses import dataclass

from app.models.common import (
    ANNOTATION_GIT_BRANCH,
    ANNOTATION_GIT_URL,
    ANNOTATION_HOST,
    ANNOTATION_RUNTIME,
    ANNOTATION_SIZE,
    CA_BUNDLE_VOLUME,
    Scaling,
)
from app.services.files import VolumeSpec
from app.services.labels import workload_labels

KSVC_API = "serving.knative.dev/v1"

# T-shirt sizes -> (cpu request, memory). Memory is set as request==limit (a
# hard, predictable OOM boundary); CPU is request-only (no limit, so the
# workload is never CPU-throttled). The request also lets cpu/memory HPA work.
_SIZES: dict[str, tuple[str, str]] = {
    "small": ("100m", "256Mi"),
    "medium": ("250m", "512Mi"),
    "large": ("500m", "1Gi"),
}


def _resources(size: str) -> dict:
    """Container resources for a t-shirt size (memory request==limit, cpu request-only)."""
    cpu_request, memory = _SIZES[size]
    return {
        "requests": {"cpu": cpu_request, "memory": memory},
        "limits": {"memory": memory},
    }


@dataclass
class ContainerEnv:
    """Resolved container env entry: a literal value or a secretKeyRef.

    This is the internal representation produced by ``services.env.resolve_env``;
    the public API only accepts ``name``/``value``/``secret``.
    """

    name: str
    value: str | None = None
    secret_ref: tuple[str, str] | None = None  # (secret_name, key)


def _env(env: list[ContainerEnv]) -> list[dict]:
    """Render resolved env entries as Knative container env (inline or secretKeyRef)."""
    out: list[dict] = []
    for e in env:
        if e.secret_ref is not None:
            name, key = e.secret_ref
            out.append(
                {"name": e.name, "valueFrom": {"secretKeyRef": {"name": name, "key": key}}}
            )
        else:
            out.append({"name": e.name, "value": e.value})
    return out


def _volumes(volumes: list[VolumeSpec]) -> tuple[list[dict], list[dict]]:
    """Render volume specs into (volumes, volumeMounts) for the pod.

    Multiple files share one ConfigMap/Secret volume; each volume is declared once
    but gets a mount (with its own subPath) per file.

    Returns:
        A ``(volumes, mounts)`` tuple of manifest fragments.
    """
    vols: dict[str, dict] = {}
    mounts: list[dict] = []
    for v in volumes:
        if v.kind == "secret":
            source = {"secret": {"secretName": v.source_name}}
        else:
            source = {"configMap": {"name": v.source_name}}
        vols[v.volume_name] = {"name": v.volume_name, **source}
        mounts.append(
            {
                "name": v.volume_name,
                "mountPath": v.mount_path,
                "subPath": v.sub_path,
                "readOnly": v.read_only,
            }
        )
    return list(vols.values()), mounts


def build_ksvc(
    *,
    name: str,
    group: str,
    owner: str,
    image: str,
    offering: str,
    host: str,
    env: list[ContainerEnv],
    volumes: list[VolumeSpec],
    scaling: Scaling,
    size: str = "small",
    pull_secret: str | None = None,
    runtime: str | None = None,
    git_url: str | None = None,
    branch: str | None = None,
    ca_config_map: str | None = None,
    ca_mount_path: str | None = None,
) -> dict:
    """Build the Knative Service (KSVC) manifest for a workload.

    Assembles labels/annotations, the autoscaling config, container env/volumes,
    resource sizing, the optional pull secret, and the trusted-CA mount.

    Args:
        name: The object name (``{name}-{group}``).
        group: Owning group.
        owner: Creating username.
        image: The image (or digest) to run.
        offering: "function" or "container".
        host: The external host (stamped as an annotation).
        env: Resolved container env entries.
        volumes: Resolved file volume specs.
        scaling: Autoscaling settings.
        size: Resource t-shirt size.
        pull_secret: Image pull secret name, if any.
        runtime: Function runtime annotation, if any.
        git_url: Function source repo annotation, if any.
        branch: Function source branch annotation, if any.
        ca_config_map: Trusted-CA ConfigMap to mount, if configured.
        ca_mount_path: Mount path for the trusted CA, if configured.

    Returns:
        The KSVC manifest dict.
    """
    annotations = {
        "autoscaling.knative.dev/min-scale": str(scaling.minScale),
        "autoscaling.knative.dev/max-scale": str(scaling.maxScale),
        "autoscaling.knative.dev/metric": scaling.metric,
        "autoscaling.knative.dev/target": str(scaling.effective_target),
    }
    # cpu/memory metrics need the HPA autoscaler class; concurrency/rps use the
    # default KPA, so the class annotation is omitted for them.
    if scaling.autoscaler_class:
        annotations["autoscaling.knative.dev/class"] = scaling.autoscaler_class
    labels = workload_labels(group, owner, name, offering)
    vols, mounts = _volumes(volumes)

    # Mount the trusted CA bundle so the workload trusts internal TLS.
    if ca_config_map and ca_mount_path:
        vols.append({"name": CA_BUNDLE_VOLUME, "configMap": {"name": ca_config_map}})
        mounts.append(
            {"name": CA_BUNDLE_VOLUME, "mountPath": ca_mount_path, "readOnly": True}
        )

    container: dict = {"image": image, "resources": _resources(size)}
    if env:
        container["env"] = _env(env)
    if mounts:
        container["volumeMounts"] = mounts

    pod_spec: dict = {
        "containers": [container],
    }
    if vols:
        pod_spec["volumes"] = vols
    if pull_secret:
        pod_spec["imagePullSecrets"] = [{"name": pull_secret}]

    meta_annotations = {ANNOTATION_HOST: host, ANNOTATION_SIZE: size}
    # Function build inputs (stamped so reads can report them; never the token).
    if runtime:
        meta_annotations[ANNOTATION_RUNTIME] = runtime
    if git_url:
        meta_annotations[ANNOTATION_GIT_URL] = git_url
    if branch:
        meta_annotations[ANNOTATION_GIT_BRANCH] = branch

    return {
        "apiVersion": KSVC_API,
        "kind": "Service",
        "metadata": {
            "name": name,
            "labels": labels,
            "annotations": meta_annotations,
        },
        "spec": {
            "template": {
                "metadata": {"annotations": annotations, "labels": labels},
                "spec": pod_spec,
            }
        },
    }
