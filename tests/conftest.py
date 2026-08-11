"""Shared test setup.

The runtimes ConfigMap is required at runtime
(``api.services.builder.runtimes``), so the suite provides a real file rather
than a stub: tests then exercise the same load path a deployment does, and a
change that breaks the file's shape fails here instead of in production.

Deliberately its own copy, not ``dev/runtimes.yaml``: this one is a fixture that
tests pin expectations against (the exact versions below appear in assertions),
so it must be free to differ from what a developer runs locally.
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
    defaultVersion: "1.24"
    versions: ["1.23", "1.24", "1.25"]
  - name: node
    builder: node
    versionEnv: BP_NODE_VERSION
    defaultVersion: "20"
    versions: ["18", "20", "22"]
"""


def plan_for(regions, tag, *, replicated=(), image=False, labels=None):
    """A BuildPlan covering `regions`, for the stub builders the suite uses.

    Every stub needs the same shape - one RegionBuild per region, differing only in
    whether it carries an Image - so building it here keeps them one line each.

    Args:
        regions: Region names the plan covers.
        tag: The image reference each region builds to.
        replicated: Manifests every region applies (the git Secret).
        image: Whether to include a kpack Image, for tests that exercise the
            re-tag comparison (it reads `spec.tag` off one).
        labels: Ownership labels to stamp on that Image.

    Returns:
        The plan.
    """
    from common.build import BuildPlan, RegionBuild

    def manifests():
        if not image:
            return []
        return [
            {
                "apiVersion": "kpack.io/v1alpha2",
                "kind": "Image",
                "metadata": {"name": "fn-hello-payments", "labels": dict(labels or {})},
                # A real tag: the engine compares it against the deployed
                # Image's, since kpack makes spec.tag immutable.
                "spec": {"tag": tag},
            }
        ]

    return BuildPlan(
        replicated=list(replicated),
        per_region={s: RegionBuild(tag=tag, manifests=manifests()) for s in regions},
    )


def runtime_registry(names=("python", "go", "node"), builder="python"):
    """A registry for services built directly, without the DI layer.

    FunctionService requires its registry rather than defaulting to the
    process-wide one, so tests supply it the same way api.dependencies does.
    """
    from api.services.builder.runtimes import RuntimeRegistry, RuntimeSpec

    return RuntimeRegistry([RuntimeSpec(name=n, builder=builder) for n in names])


@pytest.fixture(autouse=True)
def runtimes_file(tmp_path_factory, monkeypatch):
    """Point the API at a real runtimes file, and reset the cached settings.

    Autouse because the file is not optional: without it every service that
    resolves a runtime fails at construction, which is the intended production
    behaviour and would otherwise make most of the suite unrunnable.
    """
    from api.auth.deps import get_auth
    from api.core.config import get_settings
    from api.dependencies import get_runtimes

    path = tmp_path_factory.mktemp("runtimes") / "runtimes.yaml"
    path.write_text(RUNTIMES_YAML)
    monkeypatch.setenv("SERVERLESS_RUNTIMES_FILE", str(path))
    # All three are lru_cached; clear before and after so neither this fixture
    # nor a test that overrides the path leaks a registry - or an auth component
    # built from superseded settings - into the next test.
    get_settings.cache_clear()
    get_runtimes.cache_clear()
    get_auth.cache_clear()
    yield path
    get_settings.cache_clear()
    get_runtimes.cache_clear()
    get_auth.cache_clear()
