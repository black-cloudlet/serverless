"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.dependencies import get_workload_service
from app.routers import containers, functions, health

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Executes startup and shutdown logic.
    Everything before the 'yield' runs on startup.
    Everything after the 'yield' runs on shutdown.
    """
    get_workload_service()
    
    yield

def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    
    app = FastAPI(
        title="Serverless API",
        version=__version__,
        description="FaaS/CaaS REST API wrapping OpenShift Servelless Operator.",
        lifespan=lifespan
    )

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

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    
    settings = get_settings()
    
    uvicorn.run(
        "main:app",
        host="0.0.0.0", 
        port=settings.port
    )