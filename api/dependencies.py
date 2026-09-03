"""Cached service singletons wired into the FastAPI dependency system.

Each factory is ``lru_cache``d, so the whole graph is one instance per process,
built bottom-up the first time anything asks for it: the settings, then the
runtime registry, the multi-region :class:`Deployer` and the stream pool, then
the shared :class:`WorkloadService` that composes them, then the per-offering
services the routers are injected with. ``api.main`` calls the factories during
lifespan startup, so the graph exists before the first request and shutdown has
a handle on the pieces that own threads or sockets.

The ``Annotated`` aliases at the bottom are what the routers declare; FastAPI
resolves each to the cached instance, and a test replaces one through
``app.dependency_overrides``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from api.core.config import get_settings
from api.services.builder.kpack_backend import KpackBackend
from api.services.builder.runtimes import RuntimeRegistry, load_runtimes
from api.services.container import ContainerService
from api.services.function import FunctionService
from api.services.regions.deployer import Deployer
from api.services.streams.capacity import StreamCapacity
from api.services.workloads import WorkloadService


@lru_cache
def get_runtimes() -> RuntimeRegistry:
    """The cached runtime registry, read once from the mounted ConfigMap.

    Raises on a missing or unusable file (see ``load_runtimes``); ``api.main``
    calls it first during startup, so that failure lands on the pod.
    """
    return load_runtimes(get_settings().runtimes_file)


@lru_cache
def get_deployer() -> Deployer:
    """The cached multi-region Deployer (one set of cluster clients per process).

    Its clients are warmed during startup and closed at lifespan exit, both
    through the :class:`WorkloadService` that holds it.
    """
    return Deployer(get_settings())


@lru_cache
def get_stream_capacity() -> StreamCapacity:
    """The cached stream pool and admission gate (one per process).

    It owns the stream worker threads. ``api.main`` shuts it down first at
    lifespan exit through this accessor, without going through the service
    graph.
    """
    return StreamCapacity(get_settings().stream)


@lru_cache
def get_workload_service() -> WorkloadService:
    """The shared, offering-agnostic engine both offering services compose.

    Assembles the deployer, the kpack build backend built over the runtime
    registry, and the stream pool. Building it opens no connections.
    """
    settings = get_settings()
    return WorkloadService(
        settings,
        get_deployer(),
        KpackBackend(settings.build, get_runtimes()),
        get_stream_capacity(),
    )


@lru_cache
def get_function_service() -> FunctionService:
    """The cached FunctionService (the shared engine with the runtime registry).

    Injected into the functions router as ``FunctionDep``.
    """
    return FunctionService(get_workload_service(), get_runtimes())


@lru_cache
def get_container_service() -> ContainerService:
    """The cached ContainerService (composes the shared workload engine).

    Injected into the containers router as ``ContainerDep``.
    """
    return ContainerService(get_workload_service())


RuntimesDep = Annotated[RuntimeRegistry, Depends(get_runtimes)]
FunctionDep = Annotated[FunctionService, Depends(get_function_service)]
ContainerDep = Annotated[ContainerService, Depends(get_container_service)]
