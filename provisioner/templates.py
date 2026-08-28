"""The tenant template set: Helm-rendered manifests, loaded and rendered per group.

The chart ships final YAML with two placeholders - ``{{namespace}}`` and
``{{group}}``, the runtime facts Helm cannot know. The hash is over the raw
text, before substitution, so it names the set itself: one stamp per
ConfigMap, whatever the group.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

from common.cluster import ResourceKind

# Anything else in {{...}} is a template bug and fails at render.
_PLACEHOLDERS = ("{{namespace}}", "{{group}}")


@dataclass(frozen=True)
class TemplateSet:
    """One loaded template set: the raw sources and their content hash."""

    # (filename, raw text), sorted by filename so the hash and the render
    # order are properties of the set, not of the directory listing.
    sources: tuple[tuple[str, str], ...]
    digest: str

    @classmethod
    def load(cls, directory: str | Path) -> "TemplateSet":
        """Load the mounted template directory.

        Hidden entries are skipped - a ConfigMap mount holds the kubelet's
        ``..data`` machinery beside the keys.

        Args:
            directory: The mounted ConfigMap directory.

        Returns:
            The loaded set, possibly empty (the caller decides what that
            means).

        Raises:
            FileNotFoundError: If the directory does not exist (a broken
                mount, distinct from an empty ConfigMap).
        """
        root = Path(directory)
        sources = sorted(
            (entry.name, entry.read_text())
            for entry in root.iterdir()
            if entry.is_file() and not entry.name.startswith(".")
        )
        digest = hashlib.sha256()
        for name, text in sources:
            digest.update(name.encode())
            digest.update(b"\x00")
            digest.update(text.encode())
            digest.update(b"\x00")
        return cls(sources=tuple(sources), digest=digest.hexdigest()[:16])

    def __len__(self) -> int:
        """How many template files the set holds."""
        return len(self.sources)

    def render(self, *, namespace: str, group: str) -> list[dict]:
        """Substitute the placeholders and parse every manifest, in set order.

        A ``{{`` left after substitution is an unknown placeholder and fails
        here, file named - not as a literal inside a live NetworkPolicy.

        Args:
            namespace: The tenant namespace being converged.
            group: The owning (normalized) group.

        Returns:
            The manifests, in filename order then document order.

        Raises:
            ValueError: On a leftover placeholder, a malformed manifest, or a
                kind outside ``ResourceKind``.
        """
        manifests: list[dict] = []
        for name, text in self.sources:
            rendered = text.replace("{{namespace}}", namespace).replace("{{group}}", group)
            if "{{" in rendered:
                start = rendered.index("{{")
                raise ValueError(
                    f"template '{name}' holds an unknown placeholder near "
                    f"{rendered[start : start + 30]!r}; only "
                    f"{', '.join(_PLACEHOLDERS)} are substituted"
                )
            for doc in yaml.safe_load_all(rendered):
                if doc is None:
                    continue  # a trailing `---` separator, not a manifest
                if not isinstance(doc, dict):
                    raise ValueError(f"template '{name}' holds a non-mapping document")
                kind = doc.get("kind")
                obj_name = (doc.get("metadata") or {}).get("name")
                if not kind or not obj_name:
                    raise ValueError(f"template '{name}' holds a manifest without kind or name")
                # A set must not create what the prune could never collect.
                ResourceKind.from_kind(kind)
                manifests.append(doc)
        return manifests
