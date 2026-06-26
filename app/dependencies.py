"""Cached service singletons wired into the FastAPI dependency system."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from app.core.config import get_settings
from app.services.builder import FuncBuilder
from app.services.container import ContainerService
from app.services.deployer import Deployer
from app.services.function import FunctionService
from app.services.workloads import WorkloadService


@lru_cache
def get_deployer() -> Deployer:
    """The cached multi-site Deployer (one set of cluster clients per process)."""
    return Deployer(get_settings())


@lru_cache
def get_workload_service() -> WorkloadService:
    """The shared, offering-agnostic engine both offering services compose."""
    settings = get_settings()
    return WorkloadService(settings, get_deployer(), FuncBuilder(settings.registry))


@lru_cache
def get_function_service() -> FunctionService:
    """The cached FunctionService (composes the shared workload engine)."""
    return FunctionService(get_workload_service())


@lru_cache
def get_container_service() -> ContainerService:
    """The cached ContainerService (composes the shared workload engine)."""
    return ContainerService(get_workload_service())


FunctionDep = Annotated[FunctionService, Depends(get_function_service)]
ContainerDep = Annotated[ContainerService, Depends(get_container_service)]
