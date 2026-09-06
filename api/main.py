"""FastAPI application factory and the app's startup/shutdown sequence.

:func:`create_app` builds the app at import: logging, the offline docs and SSO
login under the base path, CORS, the request-id middleware, the error handlers
and the routers. :func:`lifespan` then runs on startup - runtime registry,
service graph, best-effort cache warmup - and, at exit, shuts the stream pool
and the cluster clients down in that order.
"""

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
from api.core.paths import api_base
from api.dependencies import get_runtimes, get_stream_capacity, get_workload_service
from api.routers import containers, functions, info, streams
from api.services.regions.deployer import Deployer
from api.services.tenant_namespace import close_client

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

    Runs SSO discovery and one connect per configured region concurrently, each
    off the event loop and bounded by the cluster connect plus read timeout. A
    failure is logged, not fatal: the cache is filled lazily on first use.

    Args:
        settings: The settings deciding what there is to warm.
        deployer: The deployer whose per-region clusters are connected.
    """
    timeout = settings.cluster_connect_timeout + settings.cluster_read_timeout
    tasks = []
    if settings.auth_enabled:
        tasks.append(_warm("SSO discovery", get_auth().warmup, timeout))
    if settings.regions:
        for cluster in deployer.clusters():
            tasks.append(_warm(f"cluster {cluster.region}", cluster.connect, timeout))
    if tasks:
        await asyncio.gather(*tasks)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Execute startup and shutdown logic.

    Everything before the ``yield`` runs on startup, in order: the runtime
    registry, the service graph, then the cache warmup. Everything after runs
    on shutdown, streams before cluster clients.

    Args:
        app: The FastAPI application (provided by the framework).
    """
    # Local config read from a mounted ConfigMap: a missing or unusable file
    # raises here and the pod fails to start.
    get_runtimes()
    service = get_workload_service()  # build the service graph (no network)
    await _warmup(get_settings(), service.deployer)  # warm caches, best effort

    yield

    # Streams first: their threads read through the cluster clients closed on
    # the next line, so the followers stop before those clients go away.
    get_stream_capacity().shutdown()
    service.deployer.close()  # release per-region cluster HTTP clients
    await close_client()  # the tenant controller's connection pool


def create_app() -> FastAPI:
    """Build and configure the FastAPI application.

    Returns:
        The application with logging, CORS, error handlers, and routers wired.
    """
    configure_logging()
    settings = get_settings()
    # Every path the app serves - endpoints, docs, OpenAPI, probes - hangs off
    # this (docs/API.md - REST API Specification).
    base_path = settings.base_path

    app = FastAPI(
        title="Serverless API",
        version=__version__,
        description="REST API for functions and containers on the OpenShift Serverless Operator.",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=f"{base_path}/openapi.json",
    )
    mount_offline_docs(app, base_path=base_path)
    if settings.auth_enabled:
        # With sso.swagger_client_secret set this also mounts the token proxy,
        # so the Swagger client can be a confidential one.
        wire_sso_login(app, settings.sso, base_path=base_path)

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
    # Under the base path like everything else; the chart builds the kubelet's
    # probe paths from the same basePath value it hands the code.
    app.include_router(health_router, prefix=base_path)
    for router in (info.router, streams.router, functions.router, containers.router):
        app.include_router(router, prefix=api_base(settings))

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
