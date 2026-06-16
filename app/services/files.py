"""Resolve a workload's ``files`` into backing resources + volume specs.

Inline files become API-managed ConfigMaps/Secrets (docs §7.3); referenced
files mount an existing resource. Pure functions — no cluster access.
"""

from __future__ import annotations

import base64
import posixpath
from dataclasses import dataclass

from app.models.common import FileMount
from app.services import resources as res


@dataclass
class VolumeSpec:
    """A volume + mount derived from a FileMount."""

    volume_name: str
    kind: str  # "configmap" | "secret"
    source_name: str
    mount_path: str
    sub_path: str | None = None  # set for single-file (inline) mounts


@dataclass
class ResolvedFiles:
    volumes: list[VolumeSpec]
    backing: list[dict]  # ConfigMap/Secret manifests the API must create


def _vol_name(workload: str, index: int) -> str:
    return f"{workload}-file-{index}"


def resolve_files(
    workload: str, group: str, owner: str, files: list[FileMount]
) -> ResolvedFiles:
    volumes: list[VolumeSpec] = []
    backing: list[dict] = []

    for i, f in enumerate(files):
        if f.source:
            kind = f.type or "configmap"
            volumes.append(
                VolumeSpec(
                    volume_name=_vol_name(workload, i),
                    kind=kind,
                    source_name=f.source,
                    mount_path=f.mountPath,
                )
            )
            continue

        # Inline upload — create a dedicated backing resource for this file.
        key = posixpath.basename(f.mountPath) or f"file-{i}"
        name = _vol_name(workload, i)
        if f.contentBase64 is not None:
            raw = base64.b64decode(f.contentBase64).decode("utf-8", "surrogateescape")
        else:
            raw = f.content or ""

        if f.secret:
            backing.append(res.build_secret(name, group, owner, {key: raw}))
            kind = "secret"
        else:
            backing.append(res.build_configmap(name, group, owner, {key: raw}))
            kind = "configmap"

        volumes.append(
            VolumeSpec(
                volume_name=name,
                kind=kind,
                source_name=name,
                mount_path=f.mountPath,
                sub_path=key,
            )
        )

    return ResolvedFiles(volumes=volumes, backing=backing)
