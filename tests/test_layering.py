"""Enforce the module layering described in ``common/__init__.py``.

These assertions are about *reach*, not style. A second service - the build
service in docs/BUILD-PIPELINE.md §8 - is meant to reuse the domain and cluster
layers without inheriting the API's web stack, and nothing else in the suite
would notice if an innocuous import quietly took that away.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

# Layer 1: importable by any service, including one that serves no HTTP and
# never reaches a cluster.
DOMAIN = [
    "common.names",
    "common.labels",
    "common.errors",
    "common.config",
    "common.contract",
    "common.kpack",
]
# Layer 2: adds the kubernetes client, still no web framework.
CLUSTER = ["common.cluster"]

FRAMEWORKS = {"fastapi", "starlette"}


def _imported_by(module: str) -> set[str]:
    """Top-level packages a fresh interpreter loads when importing ``module``.

    A subprocess, because pytest has already imported most of the tree - asking
    ``sys.modules`` in-process would always say everything is loaded.
    """
    code = (
        "import sys;"
        f"import {module};"
        "print(' '.join(sorted({m.split('.')[0] for m in sys.modules})))"
    )
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    return set(out.stdout.split())


@pytest.mark.parametrize("module", DOMAIN)
def test_domain_modules_pull_in_no_framework_and_no_cluster_client(module):
    loaded = _imported_by(module)
    assert not (loaded & FRAMEWORKS), (
        f"{module} reaches a web framework; a service that serves no HTTP has to import it"
    )
    assert "kubernetes" not in loaded, (
        f"{module} reaches the kubernetes client; a service that only renders manifests "
        "should not have to install it"
    )


@pytest.mark.parametrize("module", CLUSTER)
def test_the_cluster_layer_pulls_in_no_web_framework(module):
    loaded = _imported_by(module)
    assert not (loaded & FRAMEWORKS), (
        f"{module} reaches a web framework; applying manifests does not require serving HTTP"
    )


def test_common_never_imports_the_api():
    """The dependency runs one way: api may use common, never the reverse."""
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["grep", "-rn", "-e", "from api", "-e", "import api", "--include=*.py", "common/"],
        capture_output=True,
        text=True,
    )
    assert out.stdout == "", f"common/ imports from api/:\n{out.stdout}"
