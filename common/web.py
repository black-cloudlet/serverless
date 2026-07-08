"""Shared FastAPI web bits: health probes and offline API docs (airgap).

Every service exposes ``/healthz`` and ``/readyz`` and serves its OpenAPI docs
from vendored assets (no CDN, for airgap). Service-specific concerns (e.g. the
API's SSO "Authorize" wiring) stay in that service.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

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
