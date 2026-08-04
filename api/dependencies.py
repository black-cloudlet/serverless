"""Cached service singletons wired into the FastAPI dependency system."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from api.core.config import get_settings
from api.services.builder.kpack_backend import KpackBackend
from api.services.builder.runtimes import RuntimeRegistry, load_runtimes
from api.services.container import ContainerService
from api.services.function import FunctionService
from api.services.sites.deployer import Deployer
from api.services.workloads import WorkloadService


@lru_cache
def get_runtimes() -> RuntimeRegistry:
    """The cached runtime registry, read once from the mounted ConfigMap.

    Raises on a missing or unusable file (see ``load_runtimes``); ``api.main``
    calls it during startup so that surfaces as a failed pod rather than a 500
    on the first function request.
    """
    return load_runtimes(get_settings().runtimes_file)


@lru_cache
def get_deployer() -> Deployer:
    """The cached multi-site Deployer (one set of cluster clients per process)."""
    return Deployer(get_settings())


@lru_cache
def get_workload_service() -> WorkloadService:
    """The shared, offering-agnostic engine both offering services compose."""
    settings = get_settings()
    return WorkloadService(settings, get_deployer(), KpackBackend(settings, get_runtimes()))


@lru_cache
def get_function_service() -> FunctionService:
    """The cached FunctionService (composes the shared workload engine)."""
    return FunctionService(get_workload_service(), get_runtimes())


@lru_cache
def get_container_service() -> ContainerService:
    """The cached ContainerService (composes the shared workload engine)."""
    return ContainerService(get_workload_service())


RuntimesDep = Annotated[RuntimeRegistry, Depends(get_runtimes)]
FunctionDep = Annotated[FunctionService, Depends(get_function_service)]
ContainerDep = Annotated[ContainerService, Depends(get_container_service)]
