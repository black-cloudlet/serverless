"""Public per-offering platform-info endpoints (unauthenticated, static)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from api import __version__
from api.core.config import Settings, get_settings
from api.dependencies import RuntimesDep
from api.models.common import Scaling
from api.models.container import PORT_MAX, PORT_MIN
from api.models.info import ContainerInfoResponse, FunctionInfoResponse, PortCapability
from api.services import route as route_svc
from api.services.ksvc import workload_sizes

router = APIRouter(prefix="/api/v1", tags=["info"])


def _base(settings: Settings) -> dict:
    """The platform capabilities common to both offerings (see models.info.BaseInfo)."""
    return dict(
        version=__version__,
        sites=settings.site_names,
        sizes=workload_sizes(),
        scaling=Scaling.capabilities(),
        routeDomain=settings.route_domain,
        defaultHostTemplate=route_svc.HOST_TEMPLATE,
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
    return FunctionInfoResponse(**_base(settings), runtimes=runtimes.names())
