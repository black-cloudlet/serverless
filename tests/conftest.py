"""Shared test setup.

The runtimes ConfigMap is required at runtime (``api.services.runtimes``), so
the suite provides a real file rather than a stub: tests then exercise the same
load path a deployment does, and a change that breaks the file's shape fails
here instead of in production.
"""

from __future__ import annotations

import pytest

RUNTIMES_YAML = """\
runtimes:
  - name: python
    builder: python
    versionEnv: BP_CPYTHON_VERSION
    defaultVersion: "3.12"
    versions: ["3.11", "3.12"]
  - name: go
    builder: go
    versionEnv: BP_GO_VERSION
    defaultVersion: "1.22"
    versions: ["1.21", "1.22"]
  - name: node
    builder: node
    versionEnv: BP_NODE_VERSION
    defaultVersion: "20"
    versions: ["18", "20", "22"]
"""


def runtime_registry(names=("python", "go", "node"), builder="python"):
    """A registry for services built directly, without the DI layer.

    FunctionService requires its registry rather than defaulting to the
    process-wide one, so tests supply it the same way api.dependencies does.
    """
    from api.services.runtimes import RuntimeRegistry, RuntimeSpec

    return RuntimeRegistry([RuntimeSpec(name=n, builder=builder) for n in names])


@pytest.fixture(autouse=True)
def runtimes_file(tmp_path_factory, monkeypatch):
    """Point the API at a real runtimes file, and reset the cached settings.

    Autouse because the file is not optional: without it every service that
    resolves a runtime fails at construction, which is the intended production
    behaviour and would otherwise make most of the suite unrunnable.
    """
    from api.core.config import get_settings
    from api.dependencies import get_runtimes

    path = tmp_path_factory.mktemp("runtimes") / "runtimes.yaml"
    path.write_text(RUNTIMES_YAML)
    monkeypatch.setenv("SERVERLESS_RUNTIMES_FILE", str(path))
    # Both are lru_cached; clear before and after so neither this fixture nor a
    # test that overrides the path leaks a registry into the next test.
    get_settings.cache_clear()
    get_runtimes.cache_clear()
    yield path
    get_settings.cache_clear()
    get_runtimes.cache_clear()
