"""FastAPI application factory."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.auth.deps import get_validator
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.dependencies import get_workload_service
from app.docs import mount_offline_docs, wire_sso_login
from app.routers import containers, functions, health
from app.services.deployer import Deployer

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
    now. A down dependency is logged, not fatal — it is retried lazily on first
    use, preserving active/active startup.
    """
    timeout = settings.cluster_connect_timeout + settings.cluster_read_timeout
    tasks = []
    if settings.auth_enabled:
        tasks.append(_warm("SSO discovery", get_validator().warmup, timeout))
    if settings.sites:
        for cluster in deployer.resolve_targets(None):
            tasks.append(_warm(f"cluster {cluster.site}", cluster.connect, timeout))
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
    service = get_workload_service()  # build the service graph (no network)
    await _warmup(get_settings(), service.deployer)  # warm caches, best effort

    yield

    service.deployer.close()  # release per-site cluster HTTP clients


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
        # Disable the CDN-backed docs; we serve them from vendored assets so the
        # API works in an airgapped cluster (see app.docs.mount_offline_docs).
        docs_url=None,
        redoc_url=None,
    )
    mount_offline_docs(app)
    if settings.auth_enabled:
        wire_sso_login(app, settings.sso)

    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            # Auth is a bearer token in the Authorization header, not cookies, so
            # credentials mode isn't needed (and it's invalid alongside "*").
            allow_credentials=False,
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

    # Bind all interfaces: this is the container entrypoint; the pod network
    # namespace is the isolation boundary, not the bind address.
    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port)  # noqa: S104
