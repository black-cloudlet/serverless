"""The tenant template set: Helm-rendered manifests, loaded and rendered per group.

The chart ships final YAML with two placeholders - ``{{namespace}}`` and
``{{group}}``, the runtime facts Helm cannot know. The hash is over the raw
text, so it names the set itself: one stamp per ConfigMap, whatever the group.

A set is read, validated and parsed once, at load: every placeholder becomes a
YAML-safe sentinel *before* parsing - so a template may write ``{{namespace}}``
unquoted, as YAML authors do - and rendering per namespace is then a walk over
the parsed docs swapping sentinels for values. Everything a bad set can be
caught for is caught here, into the loop's backoff, before a namespace is
touched.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from common.cluster import ResourceKind

# The runtime facts a template may name. Each is swapped for a sentinel that
# parses as an ordinary YAML scalar, so an unquoted `name: {{namespace}}` is
# legal; render swaps the sentinels back. Any other `{{token}}` of this shape
# is a template bug and fails at load; braces that are not this shape (a
# Go-template payload in a ConfigMap) pass through untouched.
#
# `region` and `registry` are what keep the SET region-neutral while its OUTPUT
# is not: both regions' charts render the same bytes, so the hash matches
# everywhere, and the values are resolved against whichever cluster is being
# written to. Without them a per-region value - a Vault path, a registry host -
# would be baked in at chart render, the two regions' sets would never share a
# hash, and a provision writing a peer's namespace would write the wrong one.
PLACEHOLDERS = ("namespace", "group", "region", "registry")
_SENTINELS = {name: f"__serverless_placeholder_{name}__" for name in PLACEHOLDERS}
_TOKEN = re.compile(r"\{\{([a-z]+)\}\}")

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
    # A tenant namespace needs the region's registry credential, and ESO is how
    # every other Secret on this platform arrives. The tenant controller writes the
    # ExternalSecret; ESO fills the Secret it names.
    ResourceKind.EXTERNAL_SECRET,
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

    @property
    def renders_contents(self) -> bool:
        """Whether the set holds anything below the Namespace itself.

        The rule ``converge`` refuses on, readable without rendering: a set
        that produces only a Namespace would prune every tenant namespace
        bare, so readiness rejects it before the endpoint has to.
        """
        return any(doc.get("kind") != "Namespace" for _name, doc in self.docs)

    def render(self, *, namespace: str, group: str, region: str, registry: str) -> list[dict]:
        """Swap the sentinels for their values, in set order.

        Pure mechanism - the set was validated at load. Returns fresh
        structures on every call, so a caller may mutate them.

        Args:
            namespace: The tenant namespace being converged.
            group: The owning (normalized) group.
            region: The region of the cluster being written to - not
                necessarily this pod's own, since provisioning converges peers.
            registry: That region's registry host.

        Returns:
            The manifests, in filename order then document order.
        """
        values = {
            _SENTINELS["namespace"]: namespace,
            _SENTINELS["group"]: group,
            _SENTINELS["region"]: region,
            _SENTINELS["registry"]: registry,
        }
        return [_substitute(doc, values) for _name, doc in self.docs]


def _parse(sources: tuple[tuple[str, str], ...]) -> list[tuple[str, dict]]:
    """Substitute sentinels, then parse and validate every manifest, once.

    Raises:
        ValueError: On an unknown placeholder, YAML that does not parse, a
            malformed manifest, or a kind outside the template vocabulary
            (``TEMPLATE_KINDS``). Always ValueError, always naming the file:
            an operator reading the backoff log needs the key to fix.
    """
    docs: list[tuple[str, dict]] = []
    for name, text in sources:
        # Before parsing, so an unquoted placeholder is legal YAML - and so
        # the unknown-token check covers the whole file, comments included.
        substituted = _TOKEN.sub(lambda match, file=name: _sentinel(match, file), text)
        try:
            loaded = list(yaml.safe_load_all(substituted))
        except yaml.YAMLError as exc:
            raise ValueError(f"template '{name}' is not valid YAML: {exc}") from exc
        for doc in loaded:
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
                    f"tenant controller does not manage; allowed: "
                    f"{', '.join(sorted(_ALLOWED_KINDS))}"
                )
            docs.append((name, doc))
    return docs


def _sentinel(match: re.Match, source: str) -> str:
    """The sentinel for a matched placeholder token.

    Raises:
        ValueError: If the token is not one of ``PLACEHOLDERS``.
    """
    name = match.group(1)
    if name not in _SENTINELS:
        raise ValueError(
            f"template '{source}' holds an unknown placeholder {match.group(0)!r}; "
            f"only {', '.join('{{' + p + '}}' for p in PLACEHOLDERS)} are substituted"
        )
    return _SENTINELS[name]


def _substitute(value, values: dict[str, str]):
    """A fresh copy of ``value`` with every sentinel replaced in its strings."""
    if isinstance(value, str):
        for sentinel, replacement in values.items():
            value = value.replace(sentinel, replacement)
        return value
    if isinstance(value, dict):
        return {_substitute(k, values): _substitute(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, values) for v in value]
    return value
