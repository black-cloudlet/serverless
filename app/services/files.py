"""Resolve a workload's ``files`` into at most one ConfigMap + one Secret.

All non-secret files are aggregated into a single ``{workload}-files`` ConfigMap
and all secret files into a single ``{workload}-files`` Secret, one key per file.
Each file is mounted at its ``mountPath`` via ``subPath`` (so it appears as a
single file, not a directory). Pure functions — no cluster access.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

from app.models.common import FileMount
from app.services import resources as res
from app.services.labels import workload_labels

CONFIG_VOLUME = "files-config"
SECRET_VOLUME = "files-secret"


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
    volumes: list[VolumeSpec] = field(default_factory=list)
    backing: list[dict] = field(default_factory=list)  # at most one CM + one Secret


def files_name(workload: str) -> str:
    return f"{workload}-files"


def _key(mount_path: str) -> str:
    """Stable, collision-free ConfigMap/Secret key derived from the mount path."""
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in mount_path.lstrip("/"))
    return safe.strip("-_.") or "file"


def resolve_files(
    workload: str, group: str, owner: str, files: list[FileMount]
) -> ResolvedFiles:
    name = files_name(workload)
    labels = workload_labels(group, owner, workload)
    config_data: dict[str, str] = {}
    secret_data: dict[str, str] = {}
    volumes: list[VolumeSpec] = []
    seen: set[str] = set()

    for f in files:
        key = _key(f.mountPath)
        if key in seen:
            raise ValueError(f"duplicate file mount path resolves to key '{key}'")
        seen.add(key)

        if f.contentBase64 is not None:
            raw = base64.b64decode(f.contentBase64).decode("utf-8", "surrogateescape")
        else:
            raw = f.content or ""

        if f.secret:
            secret_data[key] = raw
            volumes.append(
                VolumeSpec(SECRET_VOLUME, "secret", name, f.mountPath, key, f.readOnly)
            )
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
