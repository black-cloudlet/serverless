"""Cached service singletons wired into the FastAPI dependency system."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from app.core.config import get_settings
from app.services.builder import FuncBuilder
from app.services.container_service import ContainerService
from app.services.deployer import Deployer
from app.services.function_service import FunctionService
from app.services.workloads import WorkloadService


@lru_cache
def get_deployer() -> Deployer:
    return Deployer(get_settings())


@lru_cache
def get_workload_service() -> WorkloadService:
    """The shared, offering-agnostic engine both offering services compose."""
    settings = get_settings()
    return WorkloadService(settings, get_deployer(), FuncBuilder(settings))


@lru_cache
def get_function_service() -> FunctionService:
    return FunctionService(get_workload_service())


@lru_cache
def get_container_service() -> ContainerService:
    return ContainerService(get_workload_service())


FunctionDep = Annotated[FunctionService, Depends(get_function_service)]
ContainerDep = Annotated[ContainerService, Depends(get_container_service)]
