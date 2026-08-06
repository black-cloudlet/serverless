"""Pure builder for a Knative Service (KSVC) manifest."""

from __future__ import annotations

from dataclasses import dataclass

from api.models.common import (
    ANNOTATION_GIT_BRANCH,
    ANNOTATION_GIT_PATH,
    ANNOTATION_GIT_URL,
    ANNOTATION_HOST,
    ANNOTATION_INJECTED_ENV,
    ANNOTATION_PULL_STAMP,
    ANNOTATION_RUNTIME,
    ANNOTATION_RUNTIME_VERSION,
    ANNOTATION_SIZE,
    CA_BUNDLE_VOLUME,
    Scaling,
)
from api.services.manifests.files import VolumeSpec
from common.labels import workload_labels

KSVC_API = "serving.knative.dev/v1"

# Point every ecosystem's CA-trust variable at the mounted bundle. Injected
# only for a name the caller did not set, and hidden from GET (see describe).
CA_ENV_VARS = (
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "GIT_SSL_CAINFO",
)

# Memory is request==limit, a predictable OOM boundary; CPU is request-only so
# it is never throttled, and the request is what cpu/memory HPA reads.
_SIZES: dict[str, tuple[str, str]] = {
    "small": ("100m", "256Mi"),
    "medium": ("250m", "512Mi"),
    "large": ("500m", "1Gi"),
}


def workload_sizes() -> list[str]:
    """The available t-shirt size names (the source of truth for /info)."""
    return list(_SIZES)


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
            out.append({"name": e.name, "valueFrom": {"secretKeyRef": {"name": name, "key": key}}})
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
    port: int | None = None,
    pull_secret: str | None = None,
    runtime: str | None = None,
    version: str | None = None,
    git_url: str | None = None,
    branch: str | None = None,
    path: str | None = None,
    ca_config_map: str | None = None,
    ca_mount_path: str | None = None,
    ca_file: str | None = None,
    pull_stamp: str | None = None,
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
        port: Container port to stamp; None uses Knative's default (8080).
        pull_secret: Image pull secret name, if any.
        runtime: Function runtime annotation, if any.
        version: Requested language version annotation, if any. Absent means the
            caller took the platform default (see services.kpack_backend).
        git_url: Function source repo annotation, if any.
        branch: Function source branch annotation, if any.
        path: Function source sub-directory annotation, if any.
        ca_config_map: Trusted-CA ConfigMap to mount, if configured.
        ca_mount_path: Mount path for the trusted CA, if configured.
        ca_file: Absolute path to the CA file inside the pod; when the CA is
            mounted, the CA-trust env vars (see ``CA_ENV_VARS``) default to it for
            any name the caller didn't set.
        pull_stamp: Carried forward, never minted here: a fresh value on every
            apply would cut a revision on every apply.

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
    # Optional: delay scale-down to smooth bursty traffic (Knative default otherwise).
    if scaling.scaleDownDelay:
        annotations["autoscaling.knative.dev/scale-down-delay"] = scaling.scaleDownDelay
    # On the template, so a change here is a change Knative cuts a revision for.
    if pull_stamp:
        annotations[ANNOTATION_PULL_STAMP] = pull_stamp
    labels = workload_labels(group, owner, name, offering)
    vols, mounts = _volumes(volumes)

    # Mount the trusted CA bundle so the workload trusts internal TLS.
    if ca_config_map and ca_mount_path:
        vols.append({"name": CA_BUNDLE_VOLUME, "configMap": {"name": ca_config_map}})
        mounts.append({"name": CA_BUNDLE_VOLUME, "mountPath": ca_mount_path, "readOnly": True})

    # Only for a name the caller did not set. Recorded in an annotation so
    # read-back hides these defaults from the spec (see services.describe).
    env_out = list(env)
    injected: list[str] = []
    if ca_config_map and ca_mount_path and ca_file:
        user_names = {e.name for e in env}
        for var in CA_ENV_VARS:
            if var not in user_names:
                env_out.append(ContainerEnv(name=var, value=ca_file))
                injected.append(var)

    container: dict = {"image": image, "resources": _resources(size)}
    # Both offerings default this to 8080, so it is normally set; None is still
    # honoured (leaving Knative its own default) for a direct caller. One port only.
    if port is not None:
        container["ports"] = [{"containerPort": port}]
    if env_out:
        container["env"] = _env(env_out)
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
    if version:
        meta_annotations[ANNOTATION_RUNTIME_VERSION] = version
    if git_url:
        meta_annotations[ANNOTATION_GIT_URL] = git_url
    if branch:
        meta_annotations[ANNOTATION_GIT_BRANCH] = branch
    if path:
        meta_annotations[ANNOTATION_GIT_PATH] = path
    if injected:
        meta_annotations[ANNOTATION_INJECTED_ENV] = ",".join(injected)
    # The stored copy an update reads back, so re-composing does not drop it.
    if pull_stamp:
        meta_annotations[ANNOTATION_PULL_STAMP] = pull_stamp

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
