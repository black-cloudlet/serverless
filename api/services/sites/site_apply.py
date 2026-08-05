"""Writing one workload into one site, and the ordering that keeps it safe.

Every function here runs off the event loop (blocking cluster I/O) and is called
through ``asyncio.to_thread`` or the deployer's fan-out.
"""

from __future__ import annotations

from api.models.common import SiteStatus
from api.services.manifests import resources as res
from api.services.manifests import secrets as secret_svc
from api.services.state import ksvc_state
from common import kpack
from common.cluster import Cluster, ResourceKind
from common.errors import NotFoundError
from common.logging import get_logger

logger = get_logger(__name__)


def apply_to_site(
    cluster: Cluster,
    *,
    oname: str,
    ksvc: dict,
    backing: list[dict],
    pull_secret_manifest: dict | None,
    mapping: dict,
    to_prune,
    created: bool,
    prev_host: str | None = None,
) -> SiteStatus:
    """Apply one workload to a single site, fail-closed (runs in a thread).

    The order is the contract: prune, then KSVC, then owner-stamped backing and
    the DomainMapping, then retire the old host. Each step depends on the one
    before it - see the comments at each.

    Raises:
        Exception: Any non-404 prune/apply error, surfaced as a per-site
            failure by the fan-out.
    """
    # Before anything goes live, so the new spec never runs beside a stale
    # Secret/ConfigMap leaking old values.
    for pkind, pname in to_prune:
        try:
            cluster.delete(pkind, pname)
        except NotFoundError:
            pass  # never existed in this site - nothing to prune

    applied = cluster.apply(ksvc)
    owner = res.owner_reference(applied[0]) if applied else None
    try:
        for manifest in backing:
            cluster.apply(res.with_owner(manifest, owner))
        if pull_secret_manifest:
            cluster.apply(res.with_owner(pull_secret_manifest, owner))
        # DomainMapping exposes the custom host; the Serverless Operator
        # auto-creates the OpenShift Route for it.
        cluster.apply(res.with_owner(mapping, owner))
    except Exception:
        # Failed after the KSVC went live. Roll back a create so no half-built
        # workload holds the name; leave an update, which is still serving.
        if created:
            try:
                cluster.delete(ResourceKind.KNATIVE_SERVICE, oname)
            except Exception:  # noqa: BLE001 - rollback is best-effort
                logger.exception("rollback of %s failed in %s", oname, cluster.site)
        raise

    # The new mapping is live, so retire the old host's. Best-effort: a leftover
    # only re-claims a host this same workload owns, and is GC'd on delete.
    new_host = mapping["metadata"]["name"]
    if not created and prev_host and prev_host != new_host:
        try:
            cluster.delete(ResourceKind.DOMAIN_MAPPING, prev_host)
        except NotFoundError:
            pass
        except Exception:  # noqa: BLE001 - old-host cleanup is best-effort
            logger.exception(
                "retiring old host %s for %s failed in %s",
                prev_host,
                oname,
                cluster.site,
            )

    # Status comes from the apply response, not a re-read. Server-side apply
    # returns the stored object - it is already trusted enough to source the
    # ownerReference every derived resource hangs off - and Knative has not
    # reconciled microseconds later, so a second GET reports the same
    # pre-reconciliation state for an extra cross-site round trip on every
    # site of every deploy. An empty response falls back to the manifest we
    # sent, which carries no status and so reads as Deploying: the right
    # answer for a workload that was just written.
    status, revision = ksvc_state.ksvc_status(applied[0] if applied else ksvc)
    return SiteStatus(site=cluster.site, status=status, revision=revision)


def apply_build_objects(
    cluster: Cluster, manifests: list[dict], *, oname: str | None = None
) -> None:
    """Apply a function's build objects on their own, outside a workload apply.

    Two callers: the create/update path when the local site runs no copy of the
    function, and the rebuild path, which never touches the KSVC.

    Raises:
        Exception: Any apply error. Failing here means the image would never
            be built, so it is surfaced rather than leaving a function whose
            tag nothing ever pushes.
    """
    owner = None
    if oname is not None:
        try:
            owner = res.owner_reference(cluster.get(ResourceKind.KNATIVE_SERVICE, oname))
        except NotFoundError:
            owner = None  # deployed elsewhere; the build is still ours to declare
    for manifest in manifests:
        cluster.apply(res.with_owner(manifest, owner))


def delete_build_objects(cluster: Cluster, oname: str) -> None:
    """Remove a function's build objects from the local site, by name.

    Best-effort: the KSVC is gone by now either way, and failing the delete
    over a build object would report a workload as undeleted when it is.
    """
    build_name = kpack.build_object_name(oname)
    for kind, obj in (
        (ResourceKind.KPACK_IMAGE, build_name),
        (ResourceKind.SERVICE_ACCOUNT, build_name),
        (ResourceKind.SECRET, secret_svc.git_secret_name(oname)),
    ):
        try:
            cluster.delete(kind, obj)
        except NotFoundError:
            pass  # owned and already cascaded, or never built here
        except Exception:  # noqa: BLE001 - a leftover is logged, not fatal
            logger.exception("could not delete %s '%s' in %s", kind, obj, cluster.site)
