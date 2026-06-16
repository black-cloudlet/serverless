"""Cached service singletons wired into the FastAPI dependency system."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from app.core.config import get_settings
from app.services.builder import FuncBuilder
from app.services.deployer import Deployer
from app.services.workloads import WorkloadService


@lru_cache
def get_deployer() -> Deployer:
    return Deployer(get_settings())


@lru_cache
def get_workload_service() -> WorkloadService:
    settings = get_settings()
    return WorkloadService(settings, get_deployer(), FuncBuilder(settings))


WorkloadDep = Annotated[WorkloadService, Depends(get_workload_service)]
