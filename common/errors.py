"""Domain errors: the shared catalog, plus the ones only this platform raises.

The base class and the general HTTP failures come from
:mod:`cloudlet_apis.errors` and are re-exported here, so this repository imports
every domain error from one module.

The classes below are ours. ``error_catalog()`` walks subclasses at call time,
so ``/info`` publishes them without the shared package knowing they exist.
"""

from __future__ import annotations

# Re-exported so this repository imports them from here - see the module docstring.
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


class ProvisioningRejectedError(APIError):
    """The tenant controller answered a provisioning request with a refusal.

    Not an outage - the controller was reachable and said no. The group already
    passed this API's own validation, so a refusal means the two ends disagree
    (suffix, token): an operator problem, not one a retry or the caller can fix.
    """

    status_code = 500
    code = "PROVISIONING_REJECTED"
