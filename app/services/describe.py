"""Read a deployed KSVC back into the desired-state spec the user submitted.

Pure functions (no cluster access). Everything here is recoverable from the KSVC
itself except non-secret file contents, which the caller supplies via
``configmaps`` (one extra read). Secret material — secret env values, secret file
contents, registry creds — is deliberately never reconstructed.
"""

from __future__ import annotations

from app.models.common import (
    ANNOTATION_GIT_BRANCH,
    ANNOTATION_GIT_URL,
    EnvVarView,
    FileView,
    Scaling,
    WorkloadSpec,
)

# Platform-injected volume mounted into every pod; not part of the user's spec.
_CA_VOLUME = "trusted-ca"


def _template(ksvc: dict) -> dict:
    return ((ksvc.get("spec") or {}).get("template") or {})


def _pod_spec(ksvc: dict) -> dict:
    return _template(ksvc).get("spec") or {}


def _container(ksvc: dict) -> dict:
    containers = _pod_spec(ksvc).get("containers") or [{}]
    return containers[0] or {}


def _annotations(ksvc: dict) -> dict:
    return (_template(ksvc).get("metadata") or {}).get("annotations") or {}


def _meta_annotations(ksvc: dict) -> dict:
    return (ksvc.get("metadata") or {}).get("annotations") or {}


def pull_secret_name(ksvc: dict) -> str | None:
    """The imagePullSecret name referenced by the pod, or None (public image)."""
    secrets = _pod_spec(ksvc).get("imagePullSecrets") or []
    return secrets[0].get("name") if secrets else None


def configmap_refs(ksvc: dict) -> set[str]:
    """ConfigMap names backing the user's (non-secret) file mounts, so the caller
    can fetch them to fill in `content`. Excludes the platform CA volume."""
    volumes = {v.get("name"): v for v in _pod_spec(ksvc).get("volumes") or []}
    names: set[str] = set()
    for mount in _container(ksvc).get("volumeMounts") or []:
        if mount.get("name") == _CA_VOLUME:
            continue
        cm = (volumes.get(mount.get("name")) or {}).get("configMap")
        if cm and cm.get("name"):
            names.add(cm["name"])
    return names


def _scaling(ksvc: dict) -> Scaling | None:
    ann = _annotations(ksvc)
    mn = ann.get("autoscaling.knative.dev/min-scale")
    mx = ann.get("autoscaling.knative.dev/max-scale")
    metric = ann.get("autoscaling.knative.dev/metric")
    target = ann.get("autoscaling.knative.dev/target")
    if mn is None or mx is None or metric is None:
        return None
    try:
        return Scaling(
            minScale=int(mn),
            maxScale=int(mx),
            metric=metric,
            target=int(target) if target is not None else None,
        )
    except Exception:  # noqa: BLE001 - unparseable annotations -> omit scaling
        return None


def _env(ksvc: dict) -> list[EnvVarView]:
    out: list[EnvVarView] = []
    for e in _container(ksvc).get("env") or []:
        if "valueFrom" in e:  # secretKeyRef -> value is redacted
            out.append(EnvVarView(name=e.get("name", ""), secret=True, value=None))
        else:
            out.append(EnvVarView(name=e.get("name", ""), value=e.get("value"), secret=False))
    return out


def _files(ksvc: dict, configmaps: dict[str, dict]) -> list[FileView]:
    volumes = {v.get("name"): v for v in _pod_spec(ksvc).get("volumes") or []}
    out: list[FileView] = []
    for mount in _container(ksvc).get("volumeMounts") or []:
        if mount.get("name") == _CA_VOLUME:
            continue
        volume = volumes.get(mount.get("name")) or {}
        is_secret = "secret" in volume
        content = None
        if not is_secret:
            cm_name = (volume.get("configMap") or {}).get("name", "")
            content = (configmaps.get(cm_name) or {}).get(mount.get("subPath"))
        out.append(
            FileView(
                mountPath=mount.get("mountPath", ""),
                readOnly=bool(mount.get("readOnly", True)),
                secret=is_secret,
                content=content,
            )
        )
    return out


def parse_spec(
    ksvc: dict,
    configmaps: dict[str, dict] | None = None,
    registry_username: str | None = None,
) -> WorkloadSpec:
    configmaps = configmaps or {}
    meta = _meta_annotations(ksvc)
    return WorkloadSpec(
        scaling=_scaling(ksvc),
        env=_env(ksvc),
        files=_files(ksvc, configmaps),
        registryUsername=registry_username,
        gitRepo=meta.get(ANNOTATION_GIT_URL),
        branch=meta.get(ANNOTATION_GIT_BRANCH),
    )
