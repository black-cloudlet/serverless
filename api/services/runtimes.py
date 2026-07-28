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
from pydantic import BaseModel, ConfigDict

from api.core.config import get_settings
from common.logging import get_logger

logger = get_logger(__name__)

# Built-in fallback when the file is absent (local dev / tests); production
# always mounts the ConfigMap.
_DEFAULT_RUNTIMES = ("python", "go", "node", "typescript")


class RuntimeSpec(BaseModel):
    """One available runtime. Only ``name`` is used today; unknown keys are kept."""

    model_config = ConfigDict(extra="allow")  # forward-compat: versions, builder, ...

    name: str


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


def load_runtimes(path: str) -> RuntimeRegistry:
    """Load the runtime registry from a YAML file, falling back to defaults.

    The file shape is ``{"runtimes": [{"name": "python"}, ...]}``. A missing file
    or an empty list falls back to the built-in defaults so local dev and tests
    run without the mounted ConfigMap.

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
