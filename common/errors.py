"""Domain errors: the shared catalog, plus the ones only this platform raises.

The base class and the general HTTP failures live in :mod:`cloudlet_apis.errors`
and are re-exported here, so every ``from common.errors import ...`` in this
repository keeps working and there is one import site to change if that ever
moves again.

What stays is what is ours: :class:`SiteTotalFailure` describes a multi-site
apply where every site failed, which no other API has. It is a plain subclass in
our own tree, and ``cloudlet_apis.errors.error_catalog()`` walks subclasses at
call time, so ``/info`` publishes it without the shared package knowing it exists.
"""

from __future__ import annotations

# Re-exported for existing importers - see the module docstring.
from cloudlet_apis.errors import (  # noqa: F401
    APIError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
    UnauthenticatedError,
    ValidationError,
    error_catalog,
)


class SiteTotalFailure(APIError):
    """All sites failed."""

    status_code = 502
    code = "SITE_TOTAL_FAILURE"
