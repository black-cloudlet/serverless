"""Available function runtimes, loaded from a mounted YAML config file.

The runtimes a function may be built with are data, not code: a ConfigMap is
mounted as a YAML file (see the Helm chart) and read here. Ops add a runtime by
editing the ConfigMap - no image rebuild. The file is intentionally minimal
today (just ``name``); extra keys (versions, builder image, ...) are preserved
so it can grow for the airgapped builder without a schema change.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from api.core.config import get_settings
from common.logging import get_logger

logger = get_logger(__name__)

# Built-in fallback when the file is absent (local dev / tests); production
# always mounts the ConfigMap. Name only - these cannot build (no Builder).
_DEFAULT_RUNTIMES = ("python", "go", "node")


class RuntimeSpec(BaseModel):
    """One available runtime, as the chart renders it into the runtimes ConfigMap.

    Every field below is read by :class:`~api.services.builder.KpackBuilder`, so
    this is the contract between the ConfigMap and the build path, not a loose
    bag of strings. ``extra="allow"`` remains for genuine forward-compatibility:
    a key this version does not know is preserved rather than rejected, so a
    newer chart can be rolled out ahead of the API.

    Numbers are coerced to strings because the ConfigMap is hand-editable YAML,
    where an unquoted ``defaultVersion: 3.12`` is a float - rejecting it would
    take the whole runtimes file down over a missing pair of quotes.

    Attributes:
        name: The runtime a caller asks for (``runtime`` on a function).
        builder: The kpack Builder that builds it. Several runtimes may share
            one. Absent means the runtime is advertised but cannot be built.
        versionEnv: Build env var that selects the language version
            (``BP_CPYTHON_VERSION``, ...).
        defaultVersion: Value for ``versionEnv`` when the caller asks for none.
        versions: Versions offered to callers. Must be a subset of what the
            pinned buildpackage ships - airgapped, there is no fallback download.
        buildEnv: Build environment, already merged by the chart (shared env,
            dependency mirror, per-runtime overrides).
        buildResources: Requests/limits for the build pod, overriding
            ``build.resources``.
    """

    model_config = ConfigDict(extra="allow", coerce_numbers_to_str=True)

    name: str
    builder: str | None = None
    versionEnv: str | None = None
    defaultVersion: str | None = None
    versions: list[str] = Field(default_factory=list)
    buildEnv: list[dict[str, str]] = Field(default_factory=list)
    buildResources: dict = Field(default_factory=dict)


class RuntimeRegistry:
    """The set of runtimes a function may request, resolved once at startup."""

    def __init__(self, specs: list[RuntimeSpec]):
        """Initialize the registry.

        Args:
            specs: The available runtime specs, in file order.
        """
        self._specs = specs

    @property
    def specs(self) -> list[RuntimeSpec]:
        """The available runtime specs."""
        return list(self._specs)

    def names(self) -> list[str]:
        """The available runtime names, in file order."""
        return [s.name for s in self._specs]

    def has(self, name: str) -> bool:
        """Whether ``name`` is an available runtime."""
        return any(s.name == name for s in self._specs)

    def get(self, name: str) -> RuntimeSpec | None:
        """The spec for ``name``, or None if it isn't an available runtime."""
        return next((s for s in self._specs if s.name == name), None)


def load_runtimes(path: str) -> RuntimeRegistry:
    """Load the runtime registry from a YAML file, falling back to defaults.

    The file shape is ``{"runtimes": [{"name": "python", ...}, ...]}`` - see
    :class:`RuntimeSpec`. A missing file or an empty list falls back to the
    built-in defaults so local dev and tests run without the mounted ConfigMap.
    Those defaults name no Builder, so they answer ``/info`` but cannot build:
    a real deployment always mounts the ConfigMap.

    Args:
        path: Path to the mounted runtimes YAML file.

    Returns:
        The resolved registry.
    """
    try:
        raw = yaml.safe_load(Path(path).read_text()) or {}
    except FileNotFoundError:
        logger.info("runtimes file %s not found; using built-in defaults", path)
        raw = {}
    items = raw.get("runtimes") or []
    specs = [RuntimeSpec(**item) for item in items]
    if not specs:
        specs = [RuntimeSpec(name=n) for n in _DEFAULT_RUNTIMES]
    return RuntimeRegistry(specs)


@lru_cache
def get_runtimes() -> RuntimeRegistry:
    """The cached runtime registry (read once from the configured file)."""
    return load_runtimes(get_settings().runtimes_file)
