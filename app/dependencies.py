"""Cached service singletons wired into the FastAPI dependency system."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from app.core.config import get_settings
from app.services.builder import FuncBuilder
from app.services.deployer import Deployer
from app.services.resource_service import ResourceService
from app.services.workloads import WorkloadService


@lru_cache
def get_deployer() -> Deployer:
    return Deployer(get_settings())


@lru_cache
def get_workload_service() -> WorkloadService:
    settings = get_settings()
    return WorkloadService(settings, get_deployer(), FuncBuilder(settings))


@lru_cache
def get_resource_service() -> ResourceService:
    return ResourceService(get_settings(), get_deployer())


WorkloadDep = Annotated[WorkloadService, Depends(get_workload_service)]
ResourceDep = Annotated[ResourceService, Depends(get_resource_service)]
