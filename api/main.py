"""FastAPI application factory."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from cloudlet_apis.auth import wire_sso_login
from cloudlet_apis.logging import configure_logging, get_logger
from cloudlet_apis.requestid import RequestIDMiddleware
from cloudlet_apis.web import health_router, mount_offline_docs, register_exception_handlers
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api import __version__
from api.auth.deps import get_auth
from api.core.config import Settings, get_settings
from api.dependencies import get_runtimes, get_stream_capacity, get_workload_service
from api.routers import containers, functions, info, streams
from api.services.regions.deployer import Deployer

logger = get_logger(__name__)


async def _warm(label: str, fn, timeout: float) -> None:
    """Run one blocking warmup off the event loop; log, never raise."""
    try:
        await asyncio.wait_for(asyncio.to_thread(fn), timeout=timeout)
        logger.info("startup warmup ok: %s", label)
    except Exception as exc:  # noqa: BLE001 - best effort; retried lazily on first use
        logger.warning("startup warmup failed for %s: %s", label, exc)


async def _warmup(settings: Settings, deployer: Deployer) -> None:
    """Warm the one-time caches (OIDC discovery, cluster connections) at startup.

    Best-effort: makes the first request fast and surfaces misconfig in the logs
    now. A down dependency is logged, not fatal - it is retried lazily on first
    use, preserving active/active startup.
    """
    timeout = settings.cluster_connect_timeout + settings.cluster_read_timeout
    tasks = []
    if settings.auth_enabled:
        tasks.append(_warm("SSO discovery", get_auth().warmup, timeout))
    if settings.regions:
        for cluster in deployer.resolve_targets(None):
            tasks.append(_warm(f"cluster {cluster.region}", cluster.connect, timeout))
    if tasks:
        await asyncio.gather(*tasks)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Execute startup and shutdown logic.

    Everything before the ``yield`` runs on startup; everything after runs on
    shutdown.

    Args:
        app: The FastAPI application (provided by the framework).
    """
    # Local config, not a remote dependency: retrying cannot fix it, and an API
    # without it would accept functions it can never build.
    get_runtimes()
    service = get_workload_service()  # build the service graph (no network)
    await _warmup(get_settings(), service.deployer)  # warm caches, best effort

    yield

    # Streams first: their threads hold the cluster clients closed below, and a
    # follower reading through a client that has just been shut under it logs a
    # traceback for what is only an orderly shutdown.
    get_stream_capacity().shutdown()
    service.deployer.close()  # release per-region cluster HTTP clients


def create_app() -> FastAPI:
    """Build and configure the FastAPI application.

    Returns:
        The application with logging, CORS, error handlers, and routers wired.
    """
    configure_logging()
    settings = get_settings()

    app = FastAPI(
        title="Serverless API",
        version=__version__,
        description="REST API for functions and containers on the OpenShift Serverless Operator.",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    mount_offline_docs(app)
    if settings.auth_enabled:
        # With sso.swagger_client_secret set this also mounts the token proxy,
        # so the Swagger client can be a confidential one.
        wire_sso_login(app, settings.sso)

    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
        )

    # Added last so it is the outermost middleware: every request (and every
    # response, including errors) carries a correlation id.
    app.add_middleware(RequestIDMiddleware)

    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(info.router)
    app.include_router(streams.router)
    app.include_router(functions.router)
    app.include_router(containers.router)

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    settings = get_settings()

    uvicorn.run(
        "api.main:app",
        host="0.0.0.0",  # noqa: S104
        port=settings.port,
        timeout_graceful_shutdown=10,
    )
