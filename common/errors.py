"""Domain errors raised by any service (docs/ARCHITECTURE.md §10).

Deliberately free of any web framework: these are raised deep in the service and
cluster layers, so importing one must not drag FastAPI into a process that
serves no HTTP - a build service raising ``NotFoundError`` should not need it.

``status_code`` and ``code`` stay here as plain data. They are how an error
describes itself, not how it is served; the FastAPI handlers that turn them into
a response envelope live in :mod:`common.web`.
"""

from __future__ import annotations

from typing import Any


class APIError(Exception):
    """Base class for errors that map to the standard error envelope."""

    status_code: int = 500
    code: str = "INTERNAL"

    def __init__(self, message: str, details: list[dict[str, Any]] | None = None):
        """Initialize the error.

        Args:
            message: Human-readable error message.
            details: Optional structured details (e.g. per-site failures).
        """
        super().__init__(message)
        self.message = message
        self.details = details or []


class ValidationError(APIError):
    """Invalid request input (HTTP 400)."""

    status_code = 400
    code = "VALIDATION_ERROR"


class UnauthenticatedError(APIError):
    """Missing or invalid credentials (HTTP 401)."""

    status_code = 401
    code = "UNAUTHENTICATED"


class ForbiddenError(APIError):
    """Authenticated but not permitted for this resource/group (HTTP 403)."""

    status_code = 403
    code = "FORBIDDEN"


class NotFoundError(APIError):
    """The requested resource does not exist (HTTP 404)."""

    status_code = 404
    code = "NOT_FOUND"


class ConflictError(APIError):
    """The request conflicts with current state, e.g. a duplicate (HTTP 409)."""

    status_code = 409
    code = "CONFLICT"


class SiteTotalFailure(APIError):
    """All sites failed."""

    status_code = 502
    code = "SITE_TOTAL_FAILURE"


class ServiceUnavailableError(APIError):
    """A required backend (e.g. the build pipeline) is not available."""

    status_code = 503
    code = "SERVICE_UNAVAILABLE"
