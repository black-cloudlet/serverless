"""Resolve a workload's ``files`` into at most one ConfigMap + one Secret.

All non-secret files are aggregated into a single ``{workload}-files`` ConfigMap
and all secret files into a single ``{workload}-files`` Secret, one key per file.
Each file is mounted at its ``mountPath`` via ``subPath`` (so it appears as a
single file, not a directory). Pure functions - no cluster access.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field

from api.models.common import FileMount
from api.services.manifests import resources as res
from common.errors import ValidationError
from common.labels import workload_labels

CONFIG_VOLUME = "files-config"
SECRET_VOLUME = "files-secret"  # noqa: S105 - a volume name, not a credential

# Kubernetes caps a whole ConfigMap/Secret object at 1MiB. Checked here, on the
# serialized manifest at accept time, because past this point an oversized
# object only fails in the background apply - a Failed region instead of a 400.
MAX_BACKING_BYTES = 1024 * 1024


@dataclass
class VolumeSpec:
    """A volume mount derived from a FileMount (always a single-file subPath).

    Always rendered read-only: Kubernetes mounts ConfigMap/Secret volumes
    read-only whatever the pod spec asks for, so there is nothing to configure.
    """

    volume_name: str
    kind: str  # "configmap" | "secret"
    source_name: str
    mount_path: str
    sub_path: str


@dataclass
class ResolvedFiles:
    """Resolved file mounts: the volume specs plus their backing objects.

    Attributes:
        volumes: One VolumeSpec per file mount.
        backing: The backing manifests (at most one ConfigMap + one Secret).
    """

    volumes: list[VolumeSpec] = field(default_factory=list)
    backing: list[dict] = field(default_factory=list)  # at most one CM + one Secret


def files_name(workload: str) -> str:
    """The shared name of a workload's files ConfigMap and Secret: ``{workload}-files``."""
    return f"{workload}-files"


def _key(mount_path: str) -> str:
    """Stable ConfigMap/Secret key derived from the mount path.

    ASCII alphanumerics only - a ConfigMap/Secret key is restricted to
    ``[-._a-zA-Z0-9]``, and ``isalnum()`` alone would let a non-ASCII letter
    through to fail the apply. Not collision-free: distinct paths can fold onto
    one key (``/etc/a/b`` and ``/etc/a-b``), which the resolver below rejects
    loudly at accept time rather than silently merging.
    """
    safe = "".join(
        c if (c.isascii() and c.isalnum()) or c in "-_." else "-" for c in mount_path.lstrip("/")
    )
    return safe.strip("-_.") or "file"


def resolve_files(
    workload: str,
    group: str,
    owner: str,
    files: list[FileMount],
    kept: dict[str, bytes] | None = None,
) -> ResolvedFiles:
    """Resolve file mounts into volume specs and backing ConfigMap/Secret.

    Non-secret files aggregate into one ConfigMap and secret files into one Secret,
    both named ``{workload}-files``, each mounted via subPath. A secret file sent
    without content is filled from ``kept``, so a redacted read can be sent back.

    Content is carried as **bytes** throughout. A file is a byte string, not text:
    ``encoding: base64`` exists so a caller can mount a keystore or a DER
    certificate, and decoding those to ``str`` on the way in only to re-encode on
    the way out cannot round-trip (it raises on the first non-UTF-8 byte). The
    backing builders take bytes and choose the right Kubernetes field.

    Args:
        workload: The object name (``{name}-{group}``).
        group: Owning group (for labels).
        owner: Username (for labels).
        files: The submitted file mounts.
        kept: Existing files-Secret content (key -> raw bytes) to fall back on
            for a secret file sent without content. Empty on create.

    Returns:
        The resolved volumes and backing manifests.

    Raises:
        ValidationError: On a duplicate mount-path key, invalid base64 content, a
            secret file sent without content when none is stored to keep, or a
            backing object over the Kubernetes 1MiB size cap.
    """
    kept = kept or {}
    name = files_name(workload)
    labels = workload_labels(group, owner, workload)
    config_data: dict[str, bytes] = {}
    secret_data: dict[str, bytes] = {}
    volumes: list[VolumeSpec] = []
    seen: dict[str, str] = {}  # key -> the mount path that claimed it

    for f in files:
        key = _key(f.mountPath)
        if key in seen:
            # Either a genuine duplicate or two distinct paths folding onto one
            # key; name both so the caller knows which mount to rename.
            raise ValidationError(
                f"file mount paths '{seen[key]}' and '{f.mountPath}' both resolve "
                f"to key '{key}'; rename one of them"
            )
        seen[key] = f.mountPath

        if f.secret and f.keep:
            # No content given: keep the stored secret content for this mount.
            if key not in kept:
                raise ValidationError(
                    f"secret file '{f.mountPath}' has no content and none is stored to keep"
                )
            raw = kept[key]
        elif f.encoding == "base64":
            try:
                # Lenient decode tolerates line-wrapped PEM bodies but still raises on bad
                # padding; both errors subclass ValueError. FileMount already rejected
                # undecodable input at the edge; this keeps non-HTTP callers honest.
                raw = base64.b64decode(f.content or "")
            except ValueError as exc:
                raise ValidationError(f"file '{f.mountPath}' has invalid base64 content") from exc
        else:
            raw = (f.content or "").encode("utf-8")

        if f.secret:
            secret_data[key] = raw
            volumes.append(VolumeSpec(SECRET_VOLUME, "secret", name, f.mountPath, key))
        else:
            config_data[key] = raw
            volumes.append(VolumeSpec(CONFIG_VOLUME, "configmap", name, f.mountPath, key))

    backing: list[dict] = []
    if config_data:
        backing.append(res.build_configmap(name, labels, config_data))
    if secret_data:
        backing.append(res.build_secret(name, labels, secret_data))
    for manifest in backing:
        # Measure what the cluster stores (Secret values and binaryData are
        # base64 there, so raw byte counts would under-report by a third).
        size = len(json.dumps(manifest, separators=(",", ":")).encode("utf-8"))
        if size > MAX_BACKING_BYTES:
            raise ValidationError(
                f"files too large: the workload's {manifest['kind']} would be "
                f"{size // 1024}KiB, over the 1MiB Kubernetes object limit"
            )
    return ResolvedFiles(volumes=volumes, backing=backing)
