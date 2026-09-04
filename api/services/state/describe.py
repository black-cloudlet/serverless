"""Read a deployed KSVC back into the desired-state spec the user submitted.

Pure functions (no cluster access). Everything here is recoverable from the KSVC
itself except non-secret file contents, which the caller supplies via
``configmaps`` (one extra read). Secret material - secret env values, secret file
contents, registry creds - is never reconstructed
(docs/FUNCTIONS.md - Redaction & keep-on-write).
"""

from __future__ import annotations

import base64

from api.models.common import (
    ANNOTATION_GIT_PATH,
    ANNOTATION_GIT_REVISION,
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
    """Convert submitted env to response views, dropping secret values.

    Args:
        env: The submitted env vars.

    Returns:
        Views with secret values set to None.
    """
    return [
        EnvVarView(name=e.name, value=None if e.secret else e.value, secret=e.secret) for e in env
    ]


def redact_files(files: list[FileMount]) -> list[FileView]:
    """Convert submitted files to response views, dropping secret contents.

    Non-secret content echoes back in canonical form: text when the bytes are
    UTF-8, else base64 with ``encoding: base64`` - the same split the live read
    makes, so either view round-trips on update
    (docs/FUNCTIONS.md - Redaction & keep-on-write).

    Args:
        files: The submitted file mounts.

    Returns:
        Views with secret file contents set to None.
    """
    out: list[FileView] = []
    for f in files:
        content: str | None = None
        encoding = "text"
        if not f.secret:
            raw = f.decoded()
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:  # binary: no text form, echo as base64
                content = base64.b64encode(raw).decode("ascii")
                encoding = "base64"
        out.append(
            FileView(mountPath=f.mountPath, secret=f.secret, content=content, encoding=encoding)
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
    """The imagePullSecret name referenced by the pod, or None (public image).

    Args:
        ksvc: The Knative Service object.

    Returns:
        The pull-secret name, or None.
    """
    secrets = _pod_spec(ksvc).get("imagePullSecrets") or []
    return secrets[0].get("name") if secrets else None


def container_port(ksvc: dict) -> int | None:
    """The explicit ``containerPort`` the user container declares, or None.

    None means no port was stamped, so the workload runs on Knative's default
    (the injected ``PORT``, 8080). Only the first declared port is read - Knative
    permits a single container port.

    Args:
        ksvc: The Knative Service object.

    Returns:
        The declared container port, or None.
    """
    ports = _container(ksvc).get("ports") or []
    if not ports:
        return None
    port = ports[0].get("containerPort")
    return port if isinstance(port, int) else None


def configmap_refs(ksvc: dict) -> set[str]:
    """ConfigMap names backing the user's (non-secret) file mounts.

    So the caller can fetch them to fill in ``content``. Excludes the platform CA
    volume.

    Args:
        ksvc: The Knative Service object.

    Returns:
        The set of backing ConfigMap names.
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
    """Read back the container env as views (secretKeyRef values redacted).

    Names listed in the injected-env annotation - the platform's CA-trust
    defaults - are omitted, since they are not part of the user's spec. A var
    the user set is never injected, so it is never listed there and reads back
    normally.
    """
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
    """Read back mounted files as views; non-secret content from ``configmaps``.

    A ``configmaps`` value is ``str`` for a file stored in the ConfigMap's
    ``data`` and ``bytes`` for one stored in ``binaryData`` (see
    ``region_read.describe_spec``); bytes read back base64-encoded with
    ``encoding: base64``, mirroring how such a file is submitted.
    """
    volumes = {v.get("name"): v for v in _pod_spec(ksvc).get("volumes") or []}
    out: list[FileView] = []
    for mount in _container(ksvc).get("volumeMounts") or []:
        if mount.get("name") == CA_BUNDLE_VOLUME:
            continue
        volume = volumes.get(mount.get("name")) or {}
        # secret-backed volumes carry a "secret" key; configMap-backed a "configMap"
        is_secret = "secret" in volume
        content = None
        encoding = "text"
        if not is_secret:
            # the mount's subPath IS the ConfigMap key (set to _key(mountPath) when
            # the volume was built), so read it straight off the manifest
            cm_name = (volume.get("configMap") or {}).get("name", "")
            value = (configmaps.get(cm_name) or {}).get(mount.get("subPath"))
            if isinstance(value, bytes):
                content = base64.b64encode(value).decode("ascii")
                encoding = "base64"
            else:
                content = value
        out.append(
            FileView(
                mountPath=mount.get("mountPath", ""),
                secret=is_secret,
                content=content,
                encoding=encoding,
            )
        )
    return out


def parse_spec(
    ksvc: dict,
    configmaps: dict[str, dict] | None = None,
    registry_username: str | None = None,
) -> WorkloadSpec:
    """Reconstruct the redacted desired-state spec from a KSVC and its ConfigMaps.

    Args:
        ksvc: The Knative Service object.
        configmaps: Fetched file ConfigMaps keyed by name, for non-secret content.
        registry_username: The pull-secret username, if read (token never shown).

    Returns:
        The reconstructed :class:`WorkloadSpec` with secrets redacted.
    """
    configmaps = configmaps or {}
    meta = _meta_annotations(ksvc)
    return WorkloadSpec(
        scaling=_scaling(ksvc),
        env=_env(ksvc),
        files=_files(ksvc, configmaps),
        # Coalesced, not passed through: a KSVC with no stamped port serves on the
        # port Knative injects when none is declared, which is DEFAULT_PORT.
        port=container_port(ksvc) or DEFAULT_PORT,
        registryUsername=registry_username,
        gitRepo=meta.get(ANNOTATION_GIT_URL),
        revision=meta.get(ANNOTATION_GIT_REVISION),
        path=meta.get(ANNOTATION_GIT_PATH),
    )
