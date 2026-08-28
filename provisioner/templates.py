"""The tenant template set: Helm-rendered manifests, loaded and rendered per group.

The chart renders the per-namespace resources into a ConfigMap of **final
YAML** - Helm has already done the real templating from values - leaving
exactly two placeholders, ``{{namespace}}`` and ``{{group}}``, which are the
runtime facts Helm cannot know. This module owns both halves of that contract:
loading the mounted set (and hashing it, so a changed set is detectable), and
rendering it for one group.

The hash is computed over the **raw** template text, before substitution, so
it names the set itself: every namespace converged from the same ConfigMap
carries the same stamp whatever its group.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

from common.cluster import ResourceKind

# The two runtime facts. Anything else in {{...}} is a template bug, and it
# fails here rather than as a string literal inside a live NetworkPolicy.
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

        Hidden entries are skipped: a mounted ConfigMap directory holds the
        kubelet's ``..data`` symlink machinery beside the keys, and only the
        keys are templates.

        Args:
            directory: The mounted ConfigMap directory.

        Returns:
            The loaded set (possibly empty - the caller decides what an empty
            set means; see :func:`provisioner.reconcile.reconcile_all`).

        Raises:
            FileNotFoundError: If the directory does not exist - a broken
                mount, distinct from a mounted-but-empty ConfigMap.
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

        Substitution is textual and happens before parsing - the templates are
        final YAML, so there is nothing structural to evaluate. A ``{{`` left
        after substitution is an unknown placeholder and fails loudly, here,
        with the file named: the alternative is a NetworkPolicy selecting the
        literal string ``{{namespce}}`` in a live cluster.

        Args:
            namespace: The tenant namespace being converged.
            group: The owning (normalized) group.

        Returns:
            The manifests, in filename order then document order.

        Raises:
            ValueError: On a leftover placeholder, a non-mapping document, a
                manifest without ``kind``/``metadata.name``, or a kind the
                platform does not operate on (``ResourceKind``).
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
                # Fails for a kind the platform has no GVK for - the same
                # registry the prune walks, so a set cannot create what the
                # prune could never collect.
                ResourceKind.from_kind(kind)
                manifests.append(doc)
        return manifests
