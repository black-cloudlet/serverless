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
from common.requestid import get_request_id

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

    FastAPI's default ``/docs`` and ``/redoc`` load JS/CSS from the jsdelivr CDN,
    which is unreachable in an airgapped cluster. Build the app with
    ``docs_url=None``/``redoc_url=None``; this mounts the vendored ``static``
    assets at ``/static`` and re-adds the docs routes pointing at them. ReDoc's
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
