"""Domain errors and the standard error envelope (docs/ARCHITECTURE.md §10)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


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


class SitePartialFailure(APIError):
    """One site failed; the deployment is Degraded."""

    status_code = 207
    code = "SITE_PARTIAL_FAILURE"


class SiteTotalFailure(APIError):
    """All sites failed."""

    status_code = 502
    code = "SITE_TOTAL_FAILURE"


class ServiceUnavailableError(APIError):
    """A required backend (e.g. the build pipeline) is not available."""

    status_code = 503
    code = "SERVICE_UNAVAILABLE"


def _envelope(
    code: str,
    message: str,
    details: list[dict[str, Any]] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build the standard error response body.

    Args:
        code: The machine-readable error code.
        message: The human-readable message.
        details: Optional structured details.
        request_id: Optional correlation id.

    Returns:
        The error envelope dict.
    """
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or [],
            "requestId": request_id,
        }
    }


def register_exception_handlers(app: FastAPI) -> None:
    """Register handlers rendering domain/HTTP errors as the standard envelope.

    Args:
        app: The FastAPI application to attach the handlers to.
    """

    @app.exception_handler(APIError)
    async def _api_error(request: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=_envelope(
                "VALIDATION_ERROR",
                "Request validation failed.",
                details=[{"loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()],
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope("HTTP_ERROR", str(exc.detail)),
        )
