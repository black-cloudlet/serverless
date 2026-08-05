"""Domain errors raised by any service (docs/ARCHITECTURE.md - REST API)."""

from __future__ import annotations

from typing import Any


class APIError(Exception):
    """Base class for errors that map to the standard error envelope."""

    status_code: int = 500
    code: str = "INTERNAL"

    def __init__(self, message: str, details: list[dict[str, Any]] | None = None):
        """Initialize the error."""
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


def error_catalog() -> list[tuple[str, int]]:
    """Every ``(code, status)`` an error envelope can carry, sorted by status."""
    seen: dict[str, int] = {APIError.code: APIError.status_code}

    def walk(cls: type[APIError]) -> None:
        for sub in cls.__subclasses__():
            seen[sub.code] = sub.status_code
            walk(sub)

    walk(APIError)
    return sorted(seen.items(), key=lambda pair: (pair[1], pair[0]))
