"""Available function runtimes, loaded from a mounted YAML config file.

The runtimes a function may be built with are data, not code: a ConfigMap is
mounted as a YAML file (see the Helm chart) and read here. Ops add a runtime by
editing the ConfigMap - no image rebuild.

Loading only: the cached, request-injectable registry is assembled in
:mod:`api.dependencies` with the platform's other singletons, so this module
stays free of FastAPI and can be reused by a service that has no HTTP layer.

The file is **required**, with no fallback: a default list would name no Builder
while looking real at the API surface, so a broken mount would pass for a working
platform until the first function failed to build.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from common.logging import get_logger

logger = get_logger(__name__)


class RuntimeConfigError(RuntimeError):
    """The runtimes file is missing or unusable - a deployment misconfiguration."""


class RuntimeSpec(BaseModel):
    """One available runtime, as the chart renders it into the runtimes ConfigMap.

    Every field is read by :class:`~api.services.builder.KpackBuilder`, so this is a
    contract with the ConfigMap, not a bag of strings. ``extra="allow"`` keeps genuine
    forward-compatibility: an unknown key is preserved, so a newer chart can ship first.

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
    """

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
    """Load the runtime registry from a YAML file.

    The file shape is ``{"runtimes": [{"name": "python", ...}, ...]}`` - see
    :class:`RuntimeSpec`.

    Loud when missing: the chart always mounts it, so absent or empty means a broken
    mount. Raising makes that a startup failure, so a misconfigured pod never passes
    readiness instead of accepting functions it can never build.

    Args:
        path: Path to the mounted runtimes YAML file.

    Returns:
        The resolved registry.

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
