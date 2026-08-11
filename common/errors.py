"""Domain errors: the shared catalog, plus the one only this platform raises.

The base class and the general HTTP failures come from
:mod:`cloudlet_apis.errors` and are re-exported here, so every
``from common.errors import ...`` in this repository keeps working.

:class:`RegionTotalFailure` is ours - a multi-region apply where every region failed.
``error_catalog()`` walks subclasses at call time, so ``/info`` publishes it
without the shared package knowing it exists.
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


class RegionTotalFailure(APIError):
    """All regions failed."""

    status_code = 502
    code = "REGION_TOTAL_FAILURE"
