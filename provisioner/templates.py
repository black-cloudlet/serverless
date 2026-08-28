"""The tenant template set: Helm-rendered manifests, loaded and rendered per group.

The chart ships final YAML with two placeholders - ``{{namespace}}`` and
``{{group}}``, the runtime facts Helm cannot know. The hash is over the raw
text, before substitution, so it names the set itself: one stamp per
ConfigMap, whatever the group.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from common.cluster import ResourceKind

# Any other lowercase {{token}} is a template bug and fails at render; braces
# that are not that shape (a Go-template payload in a ConfigMap) pass through.
_PLACEHOLDERS = ("{{namespace}}", "{{group}}")
_PLACEHOLDER_TOKEN = re.compile(r"\{\{[a-z]+\}\}")

# The template vocabulary: the namespaced kinds a set may put in a tenant
# namespace. The render gate and the prune both iterate THIS tuple, so what a
# set can create is exactly what the prune can collect - adding a kind here
# extends both in one edit.
TEMPLATE_KINDS = (
    ResourceKind.NETWORK_POLICY,
    ResourceKind.CONFIG_MAP,
    ResourceKind.ROLE_BINDING,
    ResourceKind.SECRET,
    ResourceKind.SERVICE_ACCOUNT,
)
_ALLOWED_KINDS = {k.kind for k in TEMPLATE_KINDS} | {"Namespace"}


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
        return cls.from_sources(
            (entry.name, entry.read_text())
            for entry in root.iterdir()
            if entry.is_file() and not entry.name.startswith(".")
        )

    @classmethod
    def from_sources(cls, sources) -> "TemplateSet":
        """Build a set from ``(filename, text)`` pairs - the one digest recipe.

        Args:
            sources: The pairs, in any order.

        Returns:
            The set, sorted by filename so the digest and the render order
            are properties of the content, not of the listing.
        """
        ordered = tuple(sorted(sources))
        digest = hashlib.sha256()
        for name, text in ordered:
            digest.update(name.encode())
            digest.update(b"\x00")
            digest.update(text.encode())
            digest.update(b"\x00")
        return cls(sources=ordered, digest=digest.hexdigest()[:16])

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
                kind outside the template vocabulary (``TEMPLATE_KINDS``).
        """
        manifests: list[dict] = []
        for name, text in self.sources:
            rendered = text.replace("{{namespace}}", namespace).replace("{{group}}", group)
            leftover = _PLACEHOLDER_TOKEN.search(rendered)
            if leftover:
                raise ValueError(
                    f"template '{name}' holds an unknown placeholder "
                    f"{leftover.group(0)!r}; only "
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
                if kind not in _ALLOWED_KINDS:
                    # What render admits, the prune must be able to collect.
                    raise ValueError(
                        f"template '{name}' holds kind '{kind}', which the "
                        f"provisioner does not manage; allowed: "
                        f"{', '.join(sorted(_ALLOWED_KINDS))}"
                    )
                manifests.append(doc)
        return manifests
