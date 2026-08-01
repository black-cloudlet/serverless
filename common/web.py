"""Shared FastAPI web bits: health probes, offline API docs, error handlers.

Every service exposes ``/healthz`` and ``/readyz``, serves its OpenAPI docs from
vendored assets (no CDN, for airgap), and renders :mod:`common.errors` into the
same response envelope. Service-specific concerns (e.g. the API's SSO
"Authorize" wiring) stay in that service.

This is the one place in ``common`` that requires FastAPI, which is what lets
the rest of it be imported by a service that serves no HTTP.
"""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from common.errors import APIError
from common.logging import get_logger
from common.requestid import REQUEST_ID_HEADER, get_request_id

logger = get_logger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"

health_router = APIRouter(tags=["health"])


@health_router.get("/healthz")
async def healthz() -> dict:
    """Liveness probe reporting the process is up.

    Returns:
        A constant ``{"status": "ok"}`` body.
    """
    return {"status": "ok"}


@health_router.get("/readyz")
async def readyz() -> dict:
    """Readiness probe reporting the app is ready to serve.

    Returns:
        A constant ``{"status": "ready"}`` body.
    """
    return {"status": "ready"}


def mount_offline_docs(app: FastAPI) -> None:
    """Serve Swagger UI and ReDoc from vendored assets (no CDN, for airgap).

    FastAPI's ``/docs`` and ``/redoc`` load assets from the jsdelivr CDN, unreachable
    in an airgapped cluster. Build the app with ``docs_url=None``/``redoc_url=None``;
    this mounts the vendored assets and re-adds the routes pointing at them. ReDoc's
    Google Fonts request is disabled for the same reason.

    Args:
        app: The FastAPI application to attach the offline docs to.
    """
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    @app.get("/docs", include_in_schema=False)
    async def swagger_ui_html() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title=f"{app.title} - Swagger UI",
            oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
            init_oauth=app.swagger_ui_init_oauth,
            swagger_js_url="/static/swagger-ui-bundle.js",
            swagger_css_url="/static/swagger-ui.css",
            swagger_favicon_url="/static/favicon-32x32.png",
        )

    @app.get(app.swagger_ui_oauth2_redirect_url, include_in_schema=False)
    async def swagger_ui_redirect() -> HTMLResponse:
        return get_swagger_ui_oauth2_redirect_html()

    @app.get("/redoc", include_in_schema=False)
    async def redoc_html() -> HTMLResponse:
        return get_redoc_html(
            openapi_url=app.openapi_url,
            title=f"{app.title} - ReDoc",
            redoc_js_url="/static/redoc.standalone.js",
            redoc_favicon_url="/static/favicon-32x32.png",
            with_google_fonts=False,
        )


def _envelope(
    status: int,
    code: str,
    message: str,
    details: list[dict[str, Any]] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build the standard error response body.

    Args:
        status: The numeric HTTP status code (also the response status).
        code: The machine-readable error code.
        message: The human-readable message.
        details: Optional structured details.
        request_id: Optional correlation id.

    Returns:
        The error envelope dict.
    """
    return {
        "error": {
            "status": status,
            "code": code,
            "message": message,
            "details": details or [],
            "requestId": request_id,
        }
    }


def _code_for_status(status: int) -> str:
    """A machine-readable code for a bare HTTP status (e.g. 404 -> ``NOT_FOUND``).

    Framework HTTP errors (unknown route, wrong method, ...) have no domain code,
    so derive one from the status name; fall back to ``HTTP_ERROR`` for a
    non-standard status.

    Args:
        status: The HTTP status code.

    Returns:
        The derived error code.
    """
    try:
        return HTTPStatus(status).name
    except ValueError:
        return "HTTP_ERROR"


def _request_id_of(request: Request) -> str:
    """The request's correlation id, preferring the copy stamped on the scope.

    The context var is reset as the request unwinds, and the catch-all handler
    runs *outside* the middleware that sets it - Starlette puts
    ``ServerErrorMiddleware`` outermost, so by the time it is reached the var is
    already back to ``"-"``. The scope's state survives that unwinding, which is
    why :class:`~common.requestid.RequestIDMiddleware` also exposes the id there.

    Args:
        request: The incoming request.

    Returns:
        The correlation id, or ``"-"`` if there is none.
    """
    return getattr(request.state, "request_id", None) or get_request_id()


def register_exception_handlers(app: FastAPI) -> None:
    """Register handlers rendering domain/HTTP errors as the standard envelope.

    Args:
        app: The FastAPI application to attach the handlers to.
    """

    @app.exception_handler(APIError)
    async def _api_error(request: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(
                exc.status_code, exc.code, exc.message, exc.details, request_id=get_request_id()
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=_envelope(
                400,
                "VALIDATION_ERROR",
                "Request validation failed.",
                details=[{"loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()],
                request_id=get_request_id(),
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Framework HTTP errors (unknown route, method not allowed, ...): derive a
        # meaningful code from the status instead of a flat "HTTP_ERROR".
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(
                exc.status_code,
                _code_for_status(exc.status_code),
                str(exc.detail),
                request_id=get_request_id(),
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        """Render anything unanticipated as the same envelope as everything else.

        Without this, Starlette serves an unhandled exception as plain-text
        "Internal Server Error": no ``error`` object, so a client parsing the
        envelope this API publishes on ``/info`` breaks inside its own error
        path, and no correlation id, so the report cannot be tied back to the
        traceback. A 500 is precisely the response a caller most needs to be able
        to report.

        The message is a fixed string. An exception's own text routinely carries
        internal hostnames, object names or secret material, and the caller here
        is by definition seeing something the service did not anticipate - the
        detail belongs in the log, which carries the same id.
        """
        request_id = _request_id_of(request)
        # Stamped explicitly: by now the request has unwound and the context var
        # the log filter normally reads is back to "-", so without this the one
        # line carrying the traceback is the one line the returned id can't find.
        logger.error(
            "unhandled error serving %s %s",
            request.method,
            request.url.path,
            exc_info=exc,
            extra={"request_id": request_id},
        )
        return JSONResponse(
            status_code=500,
            content=_envelope(500, APIError.code, "Internal server error.", request_id=request_id),
            # Served from ServerErrorMiddleware, which sits outside
            # RequestIDMiddleware and so never reaches the wrapper that stamps
            # this header - set it here, or a 500 is the one response with no id
            # anywhere on it.
            headers={REQUEST_ID_HEADER: request_id},
        )
