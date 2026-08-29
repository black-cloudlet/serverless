"""The provisioner's internal HTTP surface: ensure a group, plus the probes.

Internal only. There is no SSO and no browser here: the caller is the platform
API in its own namespace, reaching a Service that a NetworkPolicy scopes to it,
and the optional shared token below is depth behind that - not the primary
control.

Deliberately not built on the API's app factory. That one wires SSO, CORS,
offline docs and a base path, all of which would put the JWT stack in an image
whose whole point is that it carries less than the API does.
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
    ValidationError,
)
from common.names import Group, namespace_for_group
from provisioner.config import ProvisionerSettings
from provisioner.ensure import ensure
from provisioner.templates import TemplateSet

logger = get_logger(__name__)


class RegionResult(BaseModel):
    """One region's ensure outcome.

    Attributes:
        region: The region name.
        status: ``Ready``, ``Failed`` or ``Timeout``.
        message: The failure detail, or None when the region converged.
    """

    region: str
    status: str
    message: str | None = None


class EnsureResponse(BaseModel):
    """What ensure converged, and where.

    Attributes:
        group: The normalized group.
        namespace: The namespace the group's workloads live in. Returned
            rather than left for the caller to re-derive, so the suffix rule
            has exactly one authority.
        templateHash: The template set every listed region was converged to.
        regions: One row per region, in configured order.
    """

    group: str
    namespace: str
    templateHash: str
    regions: list[RegionResult]


def create_app(clusters: Sequence[Cluster], settings: ProvisionerSettings) -> FastAPI:
    """Build the provisioner's HTTP app over an already-built set of clusters.

    The clusters are passed in rather than built here: the reconcile loop in
    the same process holds the local one, and two sets would mean two pools of
    connections to the same API servers.

    Args:
        clusters: One cluster per configured region (the ensure fan-out).
        settings: The provisioner's settings.

    Returns:
        The configured application.
    """
    # The converges' own pool, not the server's threads: an ensure holds a
    # pool slot for as long as the slowest region takes, and a probe must
    # never have to queue behind one.
    pool = ThreadPoolExecutor(max_workers=settings.ensure_workers, thread_name_prefix="ensure")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """Drain the ensure pool before the server lets go of the process."""
        yield
        # Queued converges are dropped; a running one is left to finish, and
        # is bounded by the cluster client's own connect/read timeouts rather
        # than by anything here. Draining before the caller in main.py closes
        # the cluster clients is the point: an in-flight converge must not
        # lose the connection it is writing through.
        pool.shutdown(wait=True, cancel_futures=True)

    app = FastAPI(
        title="Serverless Provisioner",
        description="Internal API: converge a group's namespace in every region.",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)
    app.include_router(_router(clusters, settings, pool))
    return app


def _router(
    clusters: Sequence[Cluster], settings: ProvisionerSettings, pool: ThreadPoolExecutor
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
        """The mounted set, or the reason this pod cannot ensure anything.

        Readiness and ensure both go through it, so the probe cannot report
        ready on a set the endpoint would then refuse. Nothing here touches a
        cluster, per the platform's probe rule - a probe that did would take
        the pod out on someone else's outage. Every condition it does check is
        this pod's own configuration, which is exactly what readiness is for:
        a bad ConfigMap stalls its own rollout instead of failing every create.

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
            # The rule converge refuses on. Checked here too, or a truncated
            # ConfigMap would leave the pod in rotation and turn every ensure
            # into a 502 blaming the regions for this pod's own bad mount.
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

    @router.post("/ensure/{group}", response_model=EnsureResponse)
    async def ensure_group(group: Group, request: Request) -> EnsureResponse:
        """Converge the group's namespace in every region, to the current set.

        Idempotent: a group that is already converged is applied again, which
        is how the caller learns it is converged to *this* hash rather than
        last release's.

        Async, and the converges run on the app's own pool, so a slow region
        holds a pool slot rather than one of the server's threads - the
        probes stay answerable through a peer region's outage.

        Args:
            group: The owning group (from the request path, normalized).
            request: The incoming request (carries the shared token).

        Returns:
            The namespace, the hash converged to, and one row per region.

        Raises:
            ValidationError: If the group cannot name a namespace.
            UnauthenticatedError: If the shared token is set and not matched.
            ServiceUnavailableError: If this pod is not in a state to ensure
                anything (see :func:`usable_templates`).
            RegionTotalFailure: If every region failed.
        """
        _check_token(request, settings.provisioner_token)
        namespace = _namespace_for(group)
        templates = usable_templates()
        outcomes = await ensure(
            clusters,
            namespace,
            group,
            templates,
            timeout=settings.cluster_op_timeout,
            executor=pool,
        )
        rows = [RegionResult(region=o.region, status=o.status, message=o.message) for o in outcomes]
        if not any(outcome.ok for outcome in outcomes):
            # The platform's existing verdict for a fan-out where nothing
            # landed, so the API's preflight fails the create the same way a
            # total deploy failure does.
            raise RegionTotalFailure(
                f"could not ensure namespace '{namespace}' in any region",
                details=[row.model_dump() for row in rows],
            )
        return EnsureResponse(
            group=group, namespace=namespace, templateHash=templates.digest, regions=rows
        )

    return router


def _namespace_for(group: str) -> str:
    """The group's namespace, as a request-time failure rather than a 500.

    Raises:
        ValidationError: If the group is too long or reserved.
    """
    try:
        return namespace_for_group(group)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def _check_token(request: Request, configured: str) -> None:
    """Match the bearer token against the configured shared secret.

    An empty setting disables the check: the NetworkPolicy is the primary
    control, and a dev cluster has no Vault to take a token from.

    The comparison is spelled out here rather than taken from
    ``cloudlet_apis.auth``: importing anything from that package pulls in the
    JWT stack, which is precisely what this image does not ship.

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
        raise UnauthenticatedError("a valid provisioner token is required")
