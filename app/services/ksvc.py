"""Pure builder for a Knative Service (KSVC) manifest."""

from __future__ import annotations

from dataclasses import dataclass

from app.models.common import ANNOTATION_HOST, Scaling
from app.services.files import VolumeSpec
from app.services.labels import workload_labels

KSVC_API = "serving.knative.dev/v1"


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
    # Multiple files share one ConfigMap/Secret volume; declare each volume once
    # but emit a mount (with its own subPath) per file.
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
    pull_secret: str | None = None,
) -> dict:
    annotations = {
        "autoscaling.knative.dev/min-scale": str(scaling.minScale),
        "autoscaling.knative.dev/max-scale": str(scaling.maxScale),
        "autoscaling.knative.dev/target": str(scaling.targetConcurrency),
    }
    labels = workload_labels(group, owner, name, offering)
    vols, mounts = _volumes(volumes)

    container: dict = {"image": image}
    if env:
        container["env"] = _env(env)
    if mounts:
        container["volumeMounts"] = mounts

    pod_spec: dict = {
        "containerConcurrency": scaling.containerConcurrency,
        "containers": [container],
    }
    if vols:
        pod_spec["volumes"] = vols
    if pull_secret:
        pod_spec["imagePullSecrets"] = [{"name": pull_secret}]

    return {
        "apiVersion": KSVC_API,
        "kind": "Service",
        "metadata": {
            "name": name,
            "labels": labels,
            "annotations": {ANNOTATION_HOST: host},
        },
        "spec": {
            "template": {
                "metadata": {"annotations": annotations, "labels": labels},
                "spec": pod_spec,
            }
        },
    }
