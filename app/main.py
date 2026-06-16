"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from app import __version__
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.routers import containers, functions, health, resources


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="Serverless API",
        version=__version__,
        description="FaaS/CaaS REST API wrapping Knative on OpenShift.",
    )
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(functions.router)
    app.include_router(containers.router)
    app.include_router(resources.router)
    return app


app = create_app()
