"""Offline API docs: vendored Swagger UI / ReDoc and SSO login wiring.

Kept out of :mod:`app.main` so the app factory reads as just wiring. Two
concerns live here, both airgap-driven:

- serving Swagger UI and ReDoc from vendored ``app/static`` assets instead of a
  CDN (:func:`mount_offline_docs`), and
- letting Swagger UI's "Authorize" obtain an SSO token via Auth Code + PKCE
  (:func:`wire_sso_login`) — documentation/UI only; ``require_auth`` still
  enforces at runtime.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import SSOConfig

_STATIC_DIR = Path(__file__).parent / "static"


def mount_offline_docs(app: FastAPI) -> None:
    """Serve Swagger UI and ReDoc from vendored assets (no CDN, for airgap).

    FastAPI's default ``/docs`` and ``/redoc`` load JS/CSS from the jsdelivr CDN,
    which is unreachable in an airgapped cluster. The app is built with
    ``docs_url=None``/``redoc_url=None``; this mounts the vendored
    ``app/static`` assets at ``/static`` and re-adds the docs routes pointing at
    them. ReDoc's Google Fonts request is disabled for the same reason.

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


def wire_sso_login(app: FastAPI, sso: SSOConfig) -> None:
    """Let Swagger UI's "Authorize" log in via SSO (Auth Code + PKCE).

    Adds an OAuth2 security scheme to the OpenAPI (auth/token endpoints derived
    from the issuer) and configures Swagger's OAuth with the public client id.
    This is documentation/UI only — it makes Swagger obtain a token and send it
    as ``Authorization: Bearer``; ``require_auth`` still enforces at runtime, so
    the ServiceNow flow and the header are unaffected. PKCE means no secret.

    Args:
        app: The FastAPI application.
        sso: The SSO config (issuer-derived endpoints + the Swagger client id).
    """

    def custom_openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["SSO"] = {
            "type": "oauth2",
            "flows": {
                "authorizationCode": {
                    "authorizationUrl": sso.authorization_url,
                    "tokenUrl": sso.token_url,
                    "scopes": {"openid": "OpenID Connect"},
                }
            },
        }
        # Documents that endpoints take the SSO bearer token (NOT enforced by
        # FastAPI — require_auth does that).
        schema["security"] = [{"SSO": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi
    app.swagger_ui_init_oauth = {
        "clientId": sso.swagger_client_id,
        "usePkceWithAuthorizationCodeGrant": True,  # public client, no secret
        "scopes": "openid",
    }
