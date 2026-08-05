"""Available function runtimes, loaded from a mounted YAML config file."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from common.logging import get_logger

logger = get_logger(__name__)


class RuntimeConfigError(RuntimeError):
    """The runtimes file is missing or unusable - a deployment misconfiguration."""


class RuntimeSpec(BaseModel):
    """One available runtime, as the chart renders it into the runtimes ConfigMap."""

    model_config = ConfigDict(extra="allow", coerce_numbers_to_str=True)

    name: str
    builder: str | None = None
    versionEnv: str | None = None
    defaultVersion: str | None = None
    versions: list[str] = Field(default_factory=list)
    buildEnv: list[dict[str, str]] = Field(default_factory=list)


class RuntimeRegistry:
    """The set of runtimes a function may request, resolved once at startup."""

    def __init__(self, specs: list[RuntimeSpec]):
        """Initialize the registry."""
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
    """Load the runtime registry from a YAML file.

    The file shape is ``{"runtimes": [{"name": "python", ...}, ...]}`` - see
    :class:`RuntimeSpec`.

    Raises:
        RuntimeConfigError: If the file is missing, unreadable, malformed, or
            declares no runtimes.
    """
    try:
        raw = yaml.safe_load(Path(path).read_text())
    except FileNotFoundError as exc:
        raise RuntimeConfigError(
            f"runtimes file {path} not found; it is mounted from the runtimes "
            "ConfigMap and is required - check the volume mount and "
            "SERVERLESS_RUNTIMES_FILE"
        ) from exc
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeConfigError(f"runtimes file {path} could not be read: {exc}") from exc

    items = (raw or {}).get("runtimes") or []
    if not items:
        raise RuntimeConfigError(
            f"runtimes file {path} declares no runtimes; a function cannot be built "
            "without at least one runtime mapped to a kpack Builder"
        )
    try:
        specs = [RuntimeSpec(**item) for item in items]
    except (TypeError, ValidationError) as exc:
        raise RuntimeConfigError(f"runtimes file {path} is malformed: {exc}") from exc
    logger.info("loaded %d runtimes from %s: %s", len(specs), path, [s.name for s in specs])
    return RuntimeRegistry(specs)
