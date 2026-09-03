"""Build controller - rolls each finished kpack build onto the function it built.

The platform's second service (docs/BUILD-CONTROLLER.md - Digest propagation): a watch
loop over kpack Images, since a `STACK` or `BUILDPACK` rebuild finishes with no
request in flight to observe it. It serves no HTTP and takes only the domain and
cluster layers of ``common``.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: the version declared in pyproject.toml, read from
    # the installed package metadata.
    __version__ = version("serverless-api")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0.0.0"
