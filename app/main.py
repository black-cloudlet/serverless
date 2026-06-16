"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.dependencies import get_resource_service, get_workload_service
from app.routers import containers, functions, health, resources


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    app = FastAPI(
        title="Serverless API",
        version=__version__,
        description="FaaS/CaaS REST API wrapping Knative on OpenShift.",
    )

    # CORS for browser callers (e.g. a ServiceNow Service Portal widget).
    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
        )

    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(functions.router)
    app.include_router(containers.router)
    app.include_router(resources.router)

    # Build the service singletons (and thus the per-site Cluster objects) at
    # startup from config, rather than lazily on the first request.
    get_workload_service()
    get_resource_service()
    return app


app = create_app()
