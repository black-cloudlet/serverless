"""The tenant controller's internal HTTP surface: provision a group, plus the probes.

Internal only: no SSO and no browser. The caller is the platform API in its own
namespace, reaching a ClusterIP Service that a NetworkPolicy scopes to it; the
optional shared token below is a second layer behind that policy.

The app is assembled here rather than through the API's app factory, which
wires SSO, CORS, offline docs and a base path - and with them the JWT stack
this image does not ship (docs/BUILDING.md - Two images).
"""

from __future__ import annotations

import hmac
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from cloudlet_apis.logging import get_logger
from cloudlet_apis.requestid import RequestIDMiddleware
from cloudlet_apis.web import register_exception_handlers
from fastapi import APIRouter, FastAPI, Request
from pydantic import BaseModel

from common.cluster import Cluster
from common.errors import (
    RegionTotalFailure,
    ServiceUnavailableError,
    UnauthenticatedError,
)
from common.names import Group
from tenant_controller.config import TenantControllerSettings
from tenant_controller.provision import provision
from tenant_controller.templates import TemplateSet

logger = get_logger(__name__)


class RegionResult(BaseModel):
    """One region's provisioning outcome.

    Attributes:
        region: The region name.
        status: ``Ready``, ``Failed`` or ``Timeout``.
        message: The failure detail, or None when the region converged.
    """

    region: str
    status: str
    message: str | None = None


class ProvisionResponse(BaseModel):
    """What was provisioned, and where.

    Attributes:
        group: The normalized group.
        namespace: The namespace the group's workloads live in, as this
            controller derived it from the group.
        templateHash: The template set every listed region was converged to.
        regions: One row per region, in configured order.
    """

    group: str
    namespace: str
    templateHash: str
    regions: list[RegionResult]


def create_app(clusters: Sequence[Cluster], settings: TenantControllerSettings) -> FastAPI:
    """Build the tenant controller's HTTP app over an already-built set of clusters.

    Args:
        clusters: One cluster per configured region (the provision fan-out),
            the same client objects the reconcile loop in this process uses.
        settings: The tenant controller's settings.

    Returns:
        The configured application.
    """
    # The converges' own pool, not the server's threads: a provision holds a
    # pool slot for as long as its slowest region takes, so a probe answers on
    # a thread no converge occupies.
    pool = ThreadPoolExecutor(
        max_workers=settings.provision_workers, thread_name_prefix="provision"
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """Drain the provision pool before the server lets go of the process."""
        yield
        # Queued converges are dropped; a running one is left to finish, and
        # is bounded by the cluster client's own connect/read timeouts rather
        # than by anything here. The drain completes before main.py closes the
        # cluster clients, so an in-flight converge keeps the connection it is
        # writing through.
        pool.shutdown(wait=True, cancel_futures=True)

    app = FastAPI(
        title="Serverless Tenant Controller",
        description="Internal API: provision a group's namespace in every region.",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)
    app.include_router(_router(clusters, settings, pool))
    return app


def _router(
    clusters: Sequence[Cluster], settings: TenantControllerSettings, pool: ThreadPoolExecutor
) -> APIRouter:
    """The three endpoints, closed over the clusters, settings and pool."""
    router = APIRouter()

    @router.get("/healthz")
    async def healthz() -> dict:
        """Liveness: the process is up.

        Returns:
            A constant ok body.
        """
        return {"status": "ok"}

    def usable_templates() -> TemplateSet:
        """The mounted set, or the reason this pod cannot provision anything.

        Readiness and provisioning both go through it, so the probe cannot
        report ready on a set the endpoint would then refuse. Nothing here
        touches a cluster, per the platform's probe rule
        (docs/ARCHITECTURE.md - Endpoints); every condition it checks is this
        pod's own configuration, so a bad ConfigMap stalls its own rollout
        instead of failing every create.

        Returns:
            The loaded template set.

        Raises:
            ServiceUnavailableError: If no regions are configured, or the
                mounted set is missing, unusable, empty, or renders nothing
                below the Namespace. The last two count as broken, the same
                judgement the reconcile loop and converge make.
        """
        if not clusters:
            raise ServiceUnavailableError("no regions are configured")
        try:
            # Read on the event loop: the ConfigMap mount is a tmpfs holding a
            # handful of small files, capped by the 1 MiB a ConfigMap may be.
            templates = TemplateSet.load(settings.templates_dir)
        except Exception as exc:  # noqa: BLE001 - any bad set means not ready
            logger.warning("not ready: %s", exc)
            raise ServiceUnavailableError(f"template set is not usable: {exc}") from exc
        if len(templates) == 0:
            logger.warning("not ready: template set at %s is empty", settings.templates_dir)
            raise ServiceUnavailableError("template set is empty")
        if not templates.renders_contents:
            # The rule converge refuses on, checked here as well so a truncated
            # ConfigMap takes the pod out of rotation instead of turning every
            # provision into a 502 blaming the regions for this pod's mount.
            logger.warning(
                "not ready: template set at %s renders only a Namespace", settings.templates_dir
            )
            raise ServiceUnavailableError("template set renders no namespaced contents")
        return templates

    @router.get("/readyz")
    async def readyz() -> dict:
        """Readiness: this pod can converge a namespace right now.

        Returns:
            A ready body naming the loaded set.

        Raises:
            ServiceUnavailableError: See :func:`usable_templates`.
        """
        return {"status": "ready", "templateHash": usable_templates().digest}

    @router.put("/groups/{group}/namespace", response_model=ProvisionResponse)
    async def provision_group(group: Group, request: Request) -> ProvisionResponse:
        """Provision the group's namespace in every region, to the current set.

        The platform API calls this before every deploy, and retries it on
        timeout. A PUT states a desired end state: it is idempotent, and cheap
        when there is nothing to do - a namespace already stamped with the
        current template hash costs one read per region, and the response
        names the hash it carries.

        Async, and the converges run on the app's own pool, so a slow region
        holds a pool slot rather than one of the server's threads, and the
        probes stay answerable through a peer region's outage.

        A region that fails or times out gets its own row; the call succeeds
        as long as one region converged.

        Args:
            group: The owning group (from the request path, normalized).
            request: The incoming request (carries the shared token).

        Returns:
            The namespace, the hash converged to, and one row per region.

        Raises:
            ValidationError: If the group cannot name a namespace.
            UnauthenticatedError: If the shared token is set and not matched.
            ServiceUnavailableError: If this pod is not in a state to
                provision anything (see :func:`usable_templates`).
            RegionTotalFailure: If every region failed.
        """
        _check_token(request, settings.tenant_namespaces.token)
        namespace = settings.tenant_namespaces.namespace_for(group)
        templates = usable_templates()
        outcomes = await provision(
            clusters,
            namespace,
            group,
            templates,
            timeout=settings.cluster_op_timeout,
            executor=pool,
        )
        rows = [RegionResult(region=o.region, status=o.status, message=o.message) for o in outcomes]
        if not any(outcome.ok for outcome in outcomes):
            # No region landed: the same verdict a fan-out deploy raises, so
            # the API's preflight fails the create the same way.
            raise RegionTotalFailure(
                f"could not provision namespace '{namespace}' in any region",
                details=[row.model_dump() for row in rows],
            )
        return ProvisionResponse(
            group=group, namespace=namespace, templateHash=templates.digest, regions=rows
        )

    return router


def _check_token(request: Request, configured: str) -> None:
    """Match the bearer token against the configured shared secret.

    An empty setting disables the check, leaving the NetworkPolicy as the only
    control. The comparison is spelled out here rather than taken from
    ``cloudlet_apis.auth``, an import that would pull the JWT stack into this
    image (docs/BUILDING.md - Two images).

    Args:
        request: The incoming request.
        configured: The expected token; empty disables the check.

    Raises:
        UnauthenticatedError: If the header is missing, malformed, or wrong.
    """
    if not configured:
        return
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    # Compared as bytes: compare_digest raises on non-ASCII str arguments, and
    # this one comes off a header the caller controls.
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        token.encode("utf-8"), configured.encode("utf-8")
    ):
        raise UnauthenticatedError("a valid tenant-controller token is required")
