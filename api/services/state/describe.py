"""Read a deployed KSVC back into the desired-state spec the user submitted."""

from __future__ import annotations

from api.models.common import (
    ANNOTATION_GIT_BRANCH,
    ANNOTATION_GIT_PATH,
    ANNOTATION_GIT_URL,
    ANNOTATION_INJECTED_ENV,
    CA_BUNDLE_VOLUME,
    DEFAULT_PORT,
    EnvVar,
    EnvVarView,
    FileMount,
    FileView,
    Scaling,
    WorkloadSpec,
)


def redact_env(env: list[EnvVar]) -> list[EnvVarView]:
    """Convert submitted env to response views, dropping secret values."""
    return [
        EnvVarView(name=e.name, value=None if e.secret else e.value, secret=e.secret) for e in env
    ]


def redact_files(files: list[FileMount]) -> list[FileView]:
    """Convert submitted files to response views, dropping secret contents."""
    out: list[FileView] = []
    for f in files:
        content: str | None = None
        if not f.secret:
            try:
                content = f.decoded().decode("utf-8")
            except UnicodeDecodeError:
                content = None  # binary: no text form to echo
        out.append(
            FileView(mountPath=f.mountPath, readOnly=f.readOnly, secret=f.secret, content=content)
        )
    return out


def _template(ksvc: dict) -> dict:
    """The KSVC's ``spec.template`` block (or empty)."""
    return (ksvc.get("spec") or {}).get("template") or {}


def _pod_spec(ksvc: dict) -> dict:
    """The pod spec under the KSVC template (or empty)."""
    return _template(ksvc).get("spec") or {}


def _container(ksvc: dict) -> dict:
    """The first (user) container of the KSVC pod spec (or empty)."""
    containers = _pod_spec(ksvc).get("containers") or [{}]
    return containers[0] or {}


def _annotations(ksvc: dict) -> dict:
    """The template (revision) annotations of the KSVC (or empty)."""
    return (_template(ksvc).get("metadata") or {}).get("annotations") or {}


def _meta_annotations(ksvc: dict) -> dict:
    """The top-level (service) metadata annotations of the KSVC (or empty)."""
    return (ksvc.get("metadata") or {}).get("annotations") or {}


def pull_secret_name(ksvc: dict) -> str | None:
    """The imagePullSecret name referenced by the pod, or None (public image)."""
    secrets = _pod_spec(ksvc).get("imagePullSecrets") or []
    return secrets[0].get("name") if secrets else None


def container_port(ksvc: dict) -> int | None:
    """The explicit ``containerPort`` the user container declares, or None."""
    ports = _container(ksvc).get("ports") or []
    if not ports:
        return None
    port = ports[0].get("containerPort")
    return port if isinstance(port, int) else None


def configmap_refs(ksvc: dict) -> set[str]:
    """ConfigMap names backing the user's (non-secret) file mounts.

    So the caller can fetch them to fill in ``content``. Excludes the platform CA
    volume.
    """
    volumes = {v.get("name"): v for v in _pod_spec(ksvc).get("volumes") or []}
    names: set[str] = set()
    for mount in _container(ksvc).get("volumeMounts") or []:
        if mount.get("name") == CA_BUNDLE_VOLUME:
            continue
        cm = (volumes.get(mount.get("name")) or {}).get("configMap")
        if cm and cm.get("name"):
            names.add(cm["name"])
    return names


def _scaling(ksvc: dict) -> Scaling | None:
    """Reconstruct Scaling from the KSVC autoscaling annotations, or None."""
    ann = _annotations(ksvc)
    mn = ann.get("autoscaling.knative.dev/min-scale")
    mx = ann.get("autoscaling.knative.dev/max-scale")
    metric = ann.get("autoscaling.knative.dev/metric")
    target = ann.get("autoscaling.knative.dev/target")
    scale_down_delay = ann.get("autoscaling.knative.dev/scale-down-delay")
    if mn is None or mx is None or metric is None:
        return None
    try:
        return Scaling(
            minScale=int(mn),
            maxScale=int(mx),
            metric=metric,
            target=int(target) if target is not None else None,
            scaleDownDelay=scale_down_delay,
        )
    except Exception:  # noqa: BLE001 - unparseable annotations -> omit scaling
        return None


def _injected_env_names(ksvc: dict) -> set[str]:
    """Names of the platform-injected env vars (the transparent CA-trust defaults)."""
    raw = _meta_annotations(ksvc).get(ANNOTATION_INJECTED_ENV, "")
    return {n for n in raw.split(",") if n}


def _env(ksvc: dict) -> list[EnvVarView]:
    """Read back the container env as views (secretKeyRef values redacted)."""
    injected = _injected_env_names(ksvc)
    out: list[EnvVarView] = []
    for e in _container(ksvc).get("env") or []:
        name = e.get("name", "")
        if name in injected:
            continue
        if "valueFrom" in e:  # secretKeyRef -> value is redacted
            out.append(EnvVarView(name=name, secret=True, value=None))
        else:
            out.append(EnvVarView(name=name, value=e.get("value"), secret=False))
    return out


def _files(ksvc: dict, configmaps: dict[str, dict]) -> list[FileView]:
    """Read back mounted files as views; non-secret content from ``configmaps``."""
    volumes = {v.get("name"): v for v in _pod_spec(ksvc).get("volumes") or []}
    out: list[FileView] = []
    for mount in _container(ksvc).get("volumeMounts") or []:
        if mount.get("name") == CA_BUNDLE_VOLUME:
            continue
        volume = volumes.get(mount.get("name")) or {}
        # secret-backed volumes carry a "secret" key; configMap-backed a "configMap"
        is_secret = "secret" in volume
        content = None
        if not is_secret:
            # the mount's subPath IS the ConfigMap key (set to _key(mountPath) when
            # the volume was built), so read it straight off the manifest
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
    """Reconstruct the redacted desired-state spec from a KSVC and its ConfigMaps."""
    configmaps = configmaps or {}
    meta = _meta_annotations(ksvc)
    return WorkloadSpec(
        scaling=_scaling(ksvc),
        env=_env(ksvc),
        files=_files(ksvc, configmaps),
        # Coalesced, not passed through: every workload this version writes stamps
        # a port, so a missing one means the workload predates that - and 8080 is
        # genuinely the port it serves on, since that is what Knative injects when
        # none is declared. Reporting null would describe the manifest instead of
        # the workload.
        port=container_port(ksvc) or DEFAULT_PORT,
        registryUsername=registry_username,
        gitRepo=meta.get(ANNOTATION_GIT_URL),
        branch=meta.get(ANNOTATION_GIT_BRANCH),
        path=meta.get(ANNOTATION_GIT_PATH),
    )
