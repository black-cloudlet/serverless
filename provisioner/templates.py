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
    """One loaded template set: raw sources, their hash, and the parsed docs.

    Parsed and validated once at construction - a bad set fails at load, into
    the loop's backoff, before any namespace is touched - and rendered per
    namespace by substituting over the parsed structures, so a pass over N
    namespaces parses each file once instead of N times.
    """

    # (filename, raw text), sorted by filename so the hash and the render
    # order are properties of the set, not of the directory listing.
    sources: tuple[tuple[str, str], ...]
    digest: str
    # (filename, manifest) in render order, placeholders still in the values.
    docs: tuple[tuple[str, dict], ...]

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
        return cls(
            sources=ordered,
            digest=digest.hexdigest()[:16],
            docs=tuple(_parse(ordered)),
        )

    def __len__(self) -> int:
        """How many template files the set holds."""
        return len(self.sources)

    def render(self, *, namespace: str, group: str) -> list[dict]:
        """Substitute the placeholders over the parsed docs, in set order.

        A lowercase ``{{token}}`` left after substitution is an unknown
        placeholder and fails here, file named - not as a literal inside a
        live NetworkPolicy. Returns fresh structures on every call, so a
        caller may mutate them.

        Args:
            namespace: The tenant namespace being converged.
            group: The owning (normalized) group.

        Returns:
            The manifests, in filename order then document order.

        Raises:
            ValueError: On a leftover placeholder.
        """
        values = {"{{namespace}}": namespace, "{{group}}": group}
        return [_substitute(doc, values, name) for name, doc in self.docs]


def _parse(sources: tuple[tuple[str, str], ...]) -> list[tuple[str, dict]]:
    """Parse and validate every manifest once, at set construction.

    Raises:
        ValueError: On a malformed manifest or a kind outside the template
            vocabulary (``TEMPLATE_KINDS``).
    """
    docs: list[tuple[str, dict]] = []
    for name, text in sources:
        for doc in yaml.safe_load_all(text):
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
            docs.append((name, doc))
    return docs


def _substitute(value, values: dict[str, str], source: str):
    """A fresh copy of ``value`` with every placeholder replaced in its strings.

    Args:
        value: The parsed node (mapping, list, string, or scalar).
        values: Placeholder token -> replacement.
        source: The filename, for the leftover-placeholder error.

    Raises:
        ValueError: On a lowercase ``{{token}}`` that is not a placeholder.
    """
    if isinstance(value, str):
        for token, replacement in values.items():
            value = value.replace(token, replacement)
        leftover = _PLACEHOLDER_TOKEN.search(value)
        if leftover:
            raise ValueError(
                f"template '{source}' holds an unknown placeholder "
                f"{leftover.group(0)!r}; only {', '.join(_PLACEHOLDERS)} are substituted"
            )
        return value
    if isinstance(value, dict):
        return {
            _substitute(k, values, source): _substitute(v, values, source) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_substitute(v, values, source) for v in value]
    return value
