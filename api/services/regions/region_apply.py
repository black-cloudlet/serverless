"""Writing one workload into one region, and the ordering that keeps it safe.

The fan-out lives in :class:`~api.services.workloads.WorkloadService`; what
happens *inside* a single region lives here, because the ordering constraints are
local to one cluster and each has a failure it exists to prevent - a stale
Secret outliving the spec that dropped it, an orphaned resource whose owner was
never applied, a half-built create holding a name. They are stated on
:func:`apply_to_region`, next to the code that has to honour them.

Every function here runs off the event loop (blocking cluster I/O) and is called
through ``asyncio.to_thread`` or the deployer's fan-out.
"""

from __future__ import annotations

from cloudlet_apis.logging import get_logger

from api.models.common import RegionStatus
from api.services.manifests import resources as res
from api.services.manifests import secrets as secret_svc
from api.services.state import ksvc_state
from common import kpack
from common.cluster import NamespacedCluster, ResourceKind
from common.errors import NotFoundError

logger = get_logger(__name__)


def apply_to_region(
    cluster: NamespacedCluster,
    *,
    name: str,
    ksvc: dict,
    backing: list[dict],
    pull_secret_manifest: dict | None,
    mapping: dict,
    to_prune,
    created: bool,
    prev_host: str | None = None,
) -> RegionStatus:
    """Apply one workload to a single region, fail-closed (runs in a thread).

    Order matters for the no-stale-secret guarantee:

    1. **Prune first**, before anything goes live, so the new spec never runs
       beside a stale Secret/ConfigMap leaking old values.

    2. **KSVC, then owner-stamped backing, then DomainMapping.** The KSVC apply
       returns the UID the ownerReferences need, so nothing is orphaned.

    3. **Roll back a failed create, never a failed update.** A half-applied
       create is deleted so it does not hold the name and host; an update is
       left serving its last-good revision and self-heals on retry.

    4. **Retire the old host last** (update only), so it keeps serving until
       the new mapping is live, and survives a failure above.

    Args:
        cluster: The target region's cluster client.
        name: The workload's name (and its KSVC's).
        ksvc: The Knative Service manifest.
        backing: The derived backing manifests (env/files Secret/ConfigMap).
        pull_secret_manifest: The image-pull Secret manifest, if any.
        mapping: The DomainMapping manifest.
        to_prune: ``(ResourceKind, name)`` pairs to remove first.
        created: True for a create (enables rollback of the new KSVC on a
            mid-apply failure); False for an update (no destructive rollback).
        prev_host: The host the workload currently uses; when it differs from
            this apply's host, the old DomainMapping is retired after the new
            one is live (update only).

    Returns:
        The per-region status.

    Raises:
        Exception: Any non-404 prune/apply error, surfaced as a per-region
            failure by the fan-out.
    """
    for pkind, pname in to_prune:
        try:
            cluster.delete(pkind, pname)
        except NotFoundError:
            pass  # never existed in this region - nothing to prune

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
                cluster.delete(ResourceKind.KNATIVE_SERVICE, name)
            except Exception:  # noqa: BLE001 - rollback is best-effort
                logger.exception("rollback of %s failed in %s", name, cluster.region)
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
                name,
                cluster.region,
            )

    # Status comes from the apply response, not a re-read. Server-side apply
    # returns the stored object - it is already trusted enough to source the
    # ownerReference every derived resource hangs off - and Knative has not
    # reconciled microseconds later, so a second GET reports the same
    # pre-reconciliation state for an extra cross-region round trip on every
    # region of every deploy. An empty response falls back to the manifest we
    # sent, which carries no status and so reads as Deploying: the right
    # answer for a workload that was just written.
    status, revision = ksvc_state.ksvc_status(applied[0] if applied else ksvc)
    return RegionStatus(region=cluster.region, status=status, revision=revision)


def apply_build_objects(cluster: NamespacedCluster, manifests: list[dict], *, name: str) -> bool:
    """Re-declare a function's build in one region, outside a workload apply.

    The rebuild path (``POST .../build``), which touches no KSVC. A region builds
    what it runs, so an absent KSVC means this region has no build to re-declare -
    not that the objects should be applied unowned. Everything is owned by the
    KSVC beside it and cascades on delete.

    Args:
        cluster: The region to write to.
        manifests: The git Secret, build ServiceAccount and Image.
        name: The workload whose KSVC owns them.

    Returns:
        True if the objects were applied; False if the workload does not run here.

    Raises:
        Exception: Any apply error. Failing here means the image would never
            be built, so it is surfaced rather than leaving a function whose
            tag nothing ever pushes.
    """
    try:
        owner = res.owner_reference(cluster.get(ResourceKind.KNATIVE_SERVICE, name))
    except NotFoundError:
        return False
    for manifest in manifests:
        cluster.apply(res.with_owner(manifest, owner))
    return True


def delete_build_objects(cluster: NamespacedCluster, name: str) -> None:
    """Remove a function's build objects from one region, by name.

    Normally every call is a no-op 404: the objects are owned by the KSVC, so
    its delete already cascaded. It stays as the sweep for objects applied
    unowned before builds followed the workload, which nothing else collects.

    Best-effort: the KSVC is gone by now either way, and failing the delete
    over a build object would report a workload as undeleted when it is.

    Args:
        cluster: The region to clean up.
        name: The workload's name (and its KSVC's).
    """
    for kind, obj in (
        (ResourceKind.KPACK_IMAGE, kpack.build_image_name(name)),
        (ResourceKind.SERVICE_ACCOUNT, kpack.build_service_account_name(name)),
        (ResourceKind.SECRET, secret_svc.git_secret_name(name)),
    ):
        try:
            cluster.delete(kind, obj)
        except NotFoundError:
            pass  # owned and already cascaded, or never built here
        except Exception:  # noqa: BLE001 - a leftover is logged, not fatal
            logger.exception("could not delete %s '%s' in %s", kind, obj, cluster.region)
