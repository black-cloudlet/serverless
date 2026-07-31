"""SSO login wiring for Swagger UI (API-specific; airgap docs live in common.web).

Lets Swagger UI's "Authorize" obtain an SSO token via Auth Code + PKCE. This is
documentation/UI only - ``require_auth`` still enforces at runtime. The offline
Swagger/ReDoc serving is shared and lives in :func:`common.web.mount_offline_docs`.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from api.core.config import SSOConfig


def wire_sso_login(app: FastAPI, sso: SSOConfig) -> None:
    """Let Swagger UI's "Authorize" log in via SSO (Auth Code + PKCE).

    Adds an OAuth2 security scheme to the OpenAPI (auth/token endpoints derived
    from the issuer) and configures Swagger's OAuth with the public client id.
    This is documentation/UI only - it makes Swagger obtain a token and send it
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
        # FastAPI - require_auth does that).
        schema["security"] = [{"SSO": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi
    app.swagger_ui_init_oauth = {
        "clientId": sso.swagger_client_id,
        "usePkceWithAuthorizationCodeGrant": True,  # public client, no secret
        "scopes": "openid",
    }
