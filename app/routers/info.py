"""Public platform-info endpoint (unauthenticated, static)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app import __version__
from app.core.config import Settings, get_settings
from app.models.common import Scaling
from app.models.info import InfoResponse
from app.services.ksvc import workload_sizes
from app.services.runtimes import RuntimeRegistry, get_runtimes

router = APIRouter(prefix="/api/v1", tags=["info"])


@router.get("/info", response_model=InfoResponse)
async def get_info(
    settings: Annotated[Settings, Depends(get_settings)],
    runtimes: Annotated[RuntimeRegistry, Depends(get_runtimes)],
) -> InfoResponse:
    """Return static platform capabilities for dynamic UI rendering.

    Public (no auth) and config/code-derived (no cluster calls): the sites,
    runtimes, sizes, scaling options, and base domain a client needs to build a
    create form.

    Args:
        settings: Global settings (injected).
        runtimes: The available runtimes registry (injected).

    Returns:
        The platform info document.
    """
    return InfoResponse(
        version=__version__,
        sites=settings.site_names,
        runtimes=runtimes.names(),
        sizes=workload_sizes(),
        scaling=Scaling.capabilities(),
        routeDomain=settings.route_domain,
    )
