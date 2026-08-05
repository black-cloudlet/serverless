"""Build controller - rolls each finished kpack build onto the function it built.

The second of the platform's two services (docs/BUILDING.md - Ownership). The
API composes desired state in a request; this watches for a build to *finish*
and propagates its digest, which no request/response path can observe: a
``STACK`` or ``BUILDPACK`` rebuild fires with nobody asking.

It serves no HTTP and reaches only the domain and cluster layers of ``common``.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: the version declared in pyproject.toml, read from
    # the installed package metadata.
    __version__ = version("serverless-api")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0.0.0"
