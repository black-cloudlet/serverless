"""Enforce the module layering described in ``common/__init__.py``.

These assertions are about *reach*, not style. Nothing else in the suite would
notice an innocuous import taking the split away - it would just ship a
controller carrying a framework it never serves with.

Since the shared layers moved to ``cloudlet-apis``, its extras are what keep
FastAPI and pyjwt out of that image, so reaching them from a domain module
defeats the split from this side. ``common.errors`` is checked with the rest -
it re-exports from the package, which is where a heavy import would hide.
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
    "common.build",
    "common.kpack",
    "common.loop",
    # httpx, no kubernetes: the registry client both services reclaim through.
    "common.registry",
    # The shared core, reached through this repository's re-exports: bare, it
    # must stay as light as the modules that import it.
    "cloudlet_apis.errors",
    "cloudlet_apis.names",
]
# Layer 2: adds the kubernetes client, still no web framework.
CLUSTER = ["common.cluster"]

FRAMEWORKS = {"fastapi", "starlette"}
# What cloudlet-apis' [auth] extra brings. A domain module reaching these would
# put a JWT stack in the controller's image as surely as a web framework would.
AUTH_DEPS = {"jwt", "cryptography"}


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
    assert not (loaded & AUTH_DEPS), (
        f"{module} reaches cloudlet-apis' [auth] dependencies; the controller installs "
        "the package without extras and would fail to import it"
    )


@pytest.mark.parametrize("module", CLUSTER)
def test_the_cluster_layer_pulls_in_no_web_framework(module):
    loaded = _imported_by(module)
    assert not (loaded & FRAMEWORKS), (
        f"{module} reaches a web framework; applying manifests does not require serving HTTP"
    )


@pytest.mark.parametrize(
    "module", ["controller.main", "controller.reconciler", "controller.digest", "controller.gc"]
)
def test_the_build_controller_serves_no_http_and_carries_no_web_framework(module):
    loaded = _imported_by(module)
    assert not (loaded & FRAMEWORKS), (
        f"{module} reaches a web framework; the controller exposes no endpoint and "
        "should not ship one"
    )


@pytest.mark.parametrize(
    "module",
    ["tenant_controller.reconcile", "tenant_controller.templates", "tenant_controller.ensure"],
)
def test_the_tenant_controller_loop_carries_no_web_framework(module):
    """Only ``tenant_controller.api`` may reach FastAPI; the converge half must not.

    The tenant controller does serve one internal endpoint, so its image ships a web
    server - but the loop, the template set and the fan-out are what the GC and
    any future caller reuse, and none of them has a request to answer.
    """
    loaded = _imported_by(module)
    assert not (loaded & FRAMEWORKS), (
        f"{module} reaches a web framework; converging a namespace does not require serving HTTP"
    )


def test_the_tenant_controller_carries_no_auth_stack():
    """Its caller presents a shared token, so pyjwt must stay out of the image.

    ``cloudlet_apis.auth`` holds the constant-time key comparison this would
    otherwise reuse, but importing anything from that package pulls the JWT
    stack in - which is why ``tenant_controller.api`` spells the comparison out.
    """
    for module in ("tenant_controller.main", "tenant_controller.api"):
        leaked = _imported_by(module) & AUTH_DEPS
        assert not leaked, f"{module} reaches {sorted(leaked)}"


def test_the_tenant_controller_never_imports_the_api():
    """Siblings, like the controller: the fan-out is the deployer's shape, not its code."""
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "grep",
            "-rn",
            "-e",
            "from api",
            "-e",
            "import api",
            "--include=*.py",
            "tenant_controller/",
        ],
        capture_output=True,
        text=True,
    )
    assert out.stdout == "", f"tenant_controller/ imports from api/:\n{out.stdout}"


def test_the_build_controller_never_imports_the_api():
    """The two services are siblings: both use common, neither uses the other."""
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["grep", "-rn", "-e", "from api", "-e", "import api", "--include=*.py", "controller/"],
        capture_output=True,
        text=True,
    )
    assert out.stdout == "", f"controller/ imports from api/:\n{out.stdout}"


def test_the_build_controller_carries_no_auth_stack():
    """The controller installs cloudlet-apis bare, so pyjwt must never be reachable.

    The API gets it through the ``[auth]`` extra. An import in ``common`` that
    pulled ``cloudlet_apis.auth`` in would make the controller's image fail to
    start - it does not install that extra - and nothing else here would say why.
    """
    for module in (
        "controller.main",
        "controller.reconciler",
        "controller.digest",
        "controller.gc",
    ):
        leaked = _imported_by(module) & AUTH_DEPS
        assert not leaked, f"{module} reaches {sorted(leaked)}"


def test_common_never_imports_the_api():
    """The dependency runs one way: api may use common, never the reverse."""
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["grep", "-rn", "-e", "from api", "-e", "import api", "--include=*.py", "common/"],
        capture_output=True,
        text=True,
    )
    assert out.stdout == "", f"common/ imports from api/:\n{out.stdout}"
