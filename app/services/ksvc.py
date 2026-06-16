"""Pure builder for a Knative Service (KSVC) manifest."""

from __future__ import annotations

from app.models.common import EnvVar, Scaling
from app.services.files import VolumeSpec
from app.services.labels import ownership_labels

KSVC_API = "serving.knative.dev/v1"


def _env(env: list[EnvVar]) -> list[dict]:
    out: list[dict] = []
    for e in env:
        if e.value is not None:
            out.append({"name": e.name, "value": e.value})
        elif e.valueFrom is not None:
            if e.valueFrom.secret:
                ref = {"secretKeyRef": {"name": e.valueFrom.secret, "key": e.valueFrom.key}}
            else:
                ref = {"configMapKeyRef": {"name": e.valueFrom.configmap, "key": e.valueFrom.key}}
            out.append({"name": e.name, "valueFrom": ref})
    return out


def _volumes(volumes: list[VolumeSpec]) -> tuple[list[dict], list[dict]]:
    vols: list[dict] = []
    mounts: list[dict] = []
    for v in volumes:
        if v.kind == "secret":
            source = {"secret": {"secretName": v.source_name}}
        else:
            source = {"configMap": {"name": v.source_name}}
        vols.append({"name": v.volume_name, **source})
        mount = {"name": v.volume_name, "mountPath": v.mount_path}
        if v.sub_path:
            mount["subPath"] = v.sub_path
            mount["readOnly"] = True
        mounts.append(mount)
    return vols, mounts


def build_ksvc(
    *,
    name: str,
    group: str,
    owner: str,
    image: str,
    offering: str,
    env: list[EnvVar],
    volumes: list[VolumeSpec],
    scaling: Scaling,
    pull_secret: str | None = None,
) -> dict:
    annotations = {
        "autoscaling.knative.dev/min-scale": str(scaling.minScale),
        "autoscaling.knative.dev/max-scale": str(scaling.maxScale),
        "autoscaling.knative.dev/target": str(scaling.targetConcurrency),
    }
    labels = ownership_labels(group, owner, offering)
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
        "metadata": {"name": name, "labels": labels},
        "spec": {
            "template": {
                "metadata": {"annotations": annotations, "labels": labels},
                "spec": pod_spec,
            }
        },
    }
