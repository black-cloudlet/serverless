"""Public per-offering platform-info endpoints (unauthenticated, static)."""

from __future__ import annotations

from typing import Annotated, get_args

from fastapi import APIRouter, Depends

from api import __version__
from api.core.config import Settings, get_settings
from api.dependencies import RuntimesDep
from api.models.common import SITE_STATUSES, Scaling, WorkloadStatus
from api.models.container import PORT_MAX, PORT_MIN
from api.models.info import (
    ContainerInfoResponse,
    ErrorCode,
    FunctionInfoResponse,
    NamingRule,
    PortCapability,
    RuntimeCapability,
    StatusVocabulary,
)
from api.services import route as route_svc
from api.services.ksvc import workload_sizes
from common.errors import error_catalog
from common.names import MAX_OBJECT_NAME, object_name

router = APIRouter(prefix="/api/v1", tags=["info"])

# A poll ends here; every other workload status is still in flight. Derived from
# the same Literal, so a new status cannot be added without landing on one side.
TERMINAL_STATUSES = ("Ready", "Degraded")


def _base(settings: Settings) -> dict:
    """The platform capabilities common to both offerings (see models.info.BaseInfo)."""
    return dict(
        version=__version__,
        sites=settings.site_names,
        sizes=workload_sizes(),
        scaling=Scaling.capabilities(),
        routeDomain=settings.route_domain,
        defaultHostTemplate=route_svc.HOST_TEMPLATE,
        statuses=StatusVocabulary(
            workload=list(get_args(WorkloadStatus)),
            site=list(SITE_STATUSES),
            terminal=list(TERMINAL_STATUSES),
        ),
        errorCodes=[ErrorCode(code=c, status=s) for c, s in error_catalog()],
        naming=NamingRule(template=object_name("{name}", "{group}"), maxLength=MAX_OBJECT_NAME),
    )


@router.get("/containers/info", response_model=ContainerInfoResponse)
async def get_container_info(
    settings: Annotated[Settings, Depends(get_settings)],
) -> ContainerInfoResponse:
    """Return static container capabilities for dynamic UI rendering.

    Public (no auth) and config/code-derived (no cluster calls): the shared
    platform options plus the container-specific ``port`` rules a client needs to
    build a create form.

    Args:
        settings: Global settings (injected).

    Returns:
        The container info document.
    """
    return ContainerInfoResponse(
        **_base(settings),
        port=PortCapability(required=True, min=PORT_MIN, max=PORT_MAX),
    )


@router.get("/functions/info", response_model=FunctionInfoResponse)
async def get_function_info(
    settings: Annotated[Settings, Depends(get_settings)],
    runtimes: RuntimesDep,
) -> FunctionInfoResponse:
    """Return static function capabilities for dynamic UI rendering.

    Public (no auth) and config/code-derived (no cluster calls): the shared
    platform options plus the function-specific ``runtimes`` a client needs to
    build a create form.

    Args:
        settings: Global settings (injected).
        runtimes: The available runtimes registry (injected).

    Returns:
        The function info document.
    """
    return FunctionInfoResponse(
        **_base(settings),
        runtimes=[
            RuntimeCapability(
                name=spec.name,
                versions=spec.versions,
                defaultVersion=spec.defaultVersion,
            )
            for spec in runtimes.specs
        ],
    )
