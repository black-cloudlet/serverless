"""Resolve a workload's ``files`` into at most one ConfigMap + one Secret.

All non-secret files are aggregated into a single ``{workload}-files`` ConfigMap
and all secret files into a single ``{workload}-files`` Secret, one key per file.
Each file is mounted at its ``mountPath`` via ``subPath`` (so it appears as a
single file, not a directory). Pure functions - no cluster access.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

from api.models.common import FileMount
from api.services.manifests import resources as res
from common.errors import ValidationError
from common.labels import workload_labels

CONFIG_VOLUME = "files-config"
SECRET_VOLUME = "files-secret"  # noqa: S105 - a volume name, not a credential


@dataclass
class VolumeSpec:
    """A volume mount derived from a FileMount (always a single-file subPath)."""

    volume_name: str
    kind: str  # "configmap" | "secret"
    source_name: str
    mount_path: str
    sub_path: str
    read_only: bool = True


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
    """Stable, collision-free ConfigMap/Secret key derived from the mount path."""
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in mount_path.lstrip("/"))
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
    ``contentBase64`` exists so a caller can mount a keystore or a DER certificate,
    and decoding those to ``str`` on the way in only to re-encode on the way out
    cannot round-trip (it raises on the first non-UTF-8 byte). The backing builders
    take bytes and choose the right Kubernetes field.

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
        ValidationError: On a duplicate mount-path key, invalid base64 content, or a
            secret file sent without content when none is stored to keep.
    """
    kept = kept or {}
    name = files_name(workload)
    labels = workload_labels(group, owner, workload)
    config_data: dict[str, bytes] = {}
    secret_data: dict[str, bytes] = {}
    volumes: list[VolumeSpec] = []
    seen: set[str] = set()

    for f in files:
        key = _key(f.mountPath)
        if key in seen:
            raise ValidationError(
                f"duplicate file mount path '{f.mountPath}' resolves to key '{key}'"
            )
        seen.add(key)

        if f.secret and f.keep:
            # No content given: keep the stored secret content for this mount.
            if key not in kept:
                raise ValidationError(
                    f"secret file '{f.mountPath}' has no content and none is stored to keep"
                )
            raw = kept[key]
        elif f.contentBase64 is not None:
            try:
                # Lenient decode tolerates line-wrapped PEM bodies but still raises on bad
                # padding; both errors subclass ValueError. FileMount already rejected
                # undecodable input at the edge; this keeps non-HTTP callers honest.
                raw = base64.b64decode(f.contentBase64)
            except ValueError as exc:
                raise ValidationError(f"file '{f.mountPath}' has invalid base64 content") from exc
        else:
            raw = (f.content or "").encode("utf-8")

        if f.secret:
            secret_data[key] = raw
            volumes.append(VolumeSpec(SECRET_VOLUME, "secret", name, f.mountPath, key, f.readOnly))
        else:
            config_data[key] = raw
            volumes.append(
                VolumeSpec(CONFIG_VOLUME, "configmap", name, f.mountPath, key, f.readOnly)
            )

    backing: list[dict] = []
    if config_data:
        backing.append(res.build_configmap(name, labels, config_data))
    if secret_data:
        backing.append(res.build_secret(name, labels, secret_data))
    return ResolvedFiles(volumes=volumes, backing=backing)
