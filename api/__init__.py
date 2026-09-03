"""Serverless API - function and container REST API wrapping Knative on OpenShift."""

from importlib.metadata import PackageNotFoundError, version

try:
    # The version declared in pyproject.toml, read from the installed package
    # metadata.
    __version__ = version("serverless-api")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0.0.0"
