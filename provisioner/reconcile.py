"""Converging tenant namespaces to the template set, one cluster at a time.

Local-only by design, like the build controller's loop: each provisioner
converges **its own cluster** from **its own cluster's** ConfigMap, so the two
sites never fight over a namespace during Argo sync skew - each converges to
its local Git-derived state, and both end at the same hash because Git is the
single source (docs/proposals/namespace-per-group.md - multi-region).

The stamp protocol is what makes a converge crash-safe without bookkeeping:

1. Apply the Namespace **without** the hash annotation. Under server-side
   apply with our field manager this *removes* the previous stamp, marking
   the namespace mid-converge.
2. Apply the rendered contents, then prune: delete objects carrying our
   managed-by label that the current set no longer renders - the one Argo
   semantic (prune) reimplemented here, label-scoped so nothing the API or a
   tenant created can ever be collected.
3. Re-apply the Namespace **with** the hash. The stamp is the last write, so
   a crash anywhere above leaves no stamp and the next pass redoes the
   namespace; server-side apply makes redoing free.
"""

from __future__ import annotations

from cloudlet_apis.logging import get_logger

from common.cluster import Cluster, ResourceKind
from common.errors import NotFoundError
from common.labels import (
    ANNOTATION_TEMPLATE_HASH,
    LABEL_GROUP,
    LABEL_MANAGED_BY,
    PROVISIONER_VALUE,
)
from provisioner.templates import TemplateSet

logger = get_logger(__name__)

# The SSA identity every provisioner write carries. Stable, because field
# ownership is per manager: it is what lets a re-apply *remove* a label or
# annotation this component owned last time and no longer declares, while
# never touching fields the API server, the API, or an admin own.
FIELD_MANAGER = "serverless-provisioner"

# What the provisioner manages, and nothing else.
PROVISIONER_SELECTOR = f"{LABEL_MANAGED_BY}={PROVISIONER_VALUE}"

# The namespaced kinds a template set may put in a tenant namespace, and so
# the kinds the prune must sweep. Fixed rather than derived from the current
# set: a kind *removed* from the set entirely still has leftovers to collect,
# which a set-derived list would never look at again.
PRUNABLE_KINDS = (
    ResourceKind.NETWORK_POLICY,
    ResourceKind.CONFIG_MAP,
    ResourceKind.ROLE_BINDING,
    ResourceKind.SECRET,
    ResourceKind.SERVICE_ACCOUNT,
)


def converge(cluster: Cluster, namespace: str, group: str, templates: TemplateSet) -> None:
    """Bring one tenant namespace to the template set, following the stamp protocol.

    Idempotent by construction (server-side apply throughout), so an ensure
    call and the reconcile loop can both run it without coordination.

    Args:
        cluster: The cluster to converge in (cluster-scoped client; this
            function spans the namespace and the Namespace object itself).
        namespace: The tenant namespace's name. Passed rather than derived:
            the loop converges the namespace it *found*, so a changed prefix
            setting cannot strand existing namespaces under their old names.
        group: The owning (normalized) group, for the render and the labels.
        templates: The loaded template set.

    Raises:
        Exception: Any render or apply error; the caller decides whether one
            namespace's failure ends the pass (the loop continues; an ensure
            call surfaces it).
    """
    manifests = templates.render(namespace=namespace, group=group)
    ns_manifest = next((m for m in manifests if m["kind"] == "Namespace"), None)
    if ns_manifest is None:
        # A set may carry no Namespace template; the namespace itself is then
        # ours to synthesize. Labels come from _stamped like everything else.
        ns_manifest = {"apiVersion": "v1", "kind": "Namespace", "metadata": {}}
    contents = [m for m in manifests if m["kind"] != "Namespace"]

    # The namespace's name is the target's, whatever the template said: the
    # loop converges what it found, and a template naming something else would
    # otherwise create a *second* namespace mid-converge.
    ns_manifest = _stamped(ns_manifest, group)
    ns_manifest["metadata"]["name"] = namespace

    # 1. Namespace first, without the hash - clearing the old stamp marks the
    #    converge in progress (see the module docstring).
    cluster.apply(_without_hash(ns_manifest), namespace=None, field_manager=FIELD_MANAGER)

    # 2. Contents, then prune.
    keep: set[tuple[str, str]] = set()
    for manifest in contents:
        stamped = _stamped(manifest, group)
        keep.add((manifest["kind"], stamped["metadata"]["name"]))
        cluster.apply(stamped, namespace=namespace, field_manager=FIELD_MANAGER)
    _prune(cluster, namespace, keep)

    # 3. The stamp, last.
    annotations = ns_manifest["metadata"].setdefault("annotations", {})
    annotations[ANNOTATION_TEMPLATE_HASH] = templates.digest
    cluster.apply(ns_manifest, namespace=None, field_manager=FIELD_MANAGER)
    logger.info(
        "converged namespace '%s' (group '%s') to template set %s in %s",
        namespace,
        group,
        templates.digest,
        cluster.region,
    )


def reconcile_all(cluster: Cluster, templates: TemplateSet) -> tuple[int, int, int]:
    """Converge every managed namespace in this cluster whose stamp is stale.

    One namespace failing is logged and skipped, never the end of the pass -
    the listing order is stable, so an aborting error would starve every
    namespace after the failing one on every pass, deterministically
    (the same rule as ``TagGC.sweep``).

    An **empty** template set refuses to converge anything: mounted-but-empty
    is indistinguishable from a broken mount mid-update, and treating it as
    intent would prune every managed object out of every tenant namespace.
    Emptying a live set on purpose is an operation that deserves a human and
    a deliberate mechanism, not a ConfigMap race.

    Args:
        cluster: The local cluster (cluster-scoped client).
        templates: The currently mounted template set.

    Returns:
        ``(seen, converged, failed)`` counts, for the pass's log line and for
        readiness ("unconverged namespaces" is the provisioner's signal).
    """
    if len(templates) == 0:
        logger.warning(
            "template set is empty; refusing to converge (an empty mount is "
            "indistinguishable from a broken one)"
        )
        return (0, 0, 0)
    namespaces = cluster.get(
        ResourceKind.NAMESPACE, label_selector=PROVISIONER_SELECTOR, namespace=None
    )
    seen = converged = failed = 0
    for ns in namespaces:
        seen += 1
        meta = ns.get("metadata") or {}
        name = meta.get("name", "")
        group = (meta.get("labels") or {}).get(LABEL_GROUP)
        if not group:
            # Ours by managed-by but unattributable: converging would render
            # templates for a group nothing names. A human made this state.
            logger.warning("managed namespace '%s' carries no group label; skipping", name)
            failed += 1
            continue
        stamp = (meta.get("annotations") or {}).get(ANNOTATION_TEMPLATE_HASH)
        if stamp == templates.digest:
            continue
        try:
            converge(cluster, name, group, templates)
            converged += 1
        except Exception:  # noqa: BLE001 - the next namespace still gets its converge
            logger.exception("converging namespace '%s' failed; continuing", name)
            failed += 1
    logger.info(
        "reconciled %d managed namespace(s) in %s: %d converged, %d failed, set %s",
        seen,
        cluster.region,
        converged,
        failed,
        templates.digest,
    )
    return (seen, converged, failed)


def _stamped(manifest: dict, group: str) -> dict:
    """A copy of ``manifest`` carrying the provisioner's ownership labels.

    Injected in code rather than trusted to the templates: the prune and the
    GC select on these labels, and a template that forgot them would create
    objects the prune could never collect - or worse, a namespace the GC
    would never see.
    """
    meta = dict(manifest.get("metadata") or {})
    labels = dict(meta.get("labels") or {})
    labels[LABEL_MANAGED_BY] = PROVISIONER_VALUE
    labels[LABEL_GROUP] = group
    meta["labels"] = labels
    return {**manifest, "metadata": meta}


def _without_hash(ns_manifest: dict) -> dict:
    """The namespace manifest with no template-hash annotation declared.

    Under SSA with our field manager, applying this *removes* a previously
    stamped hash - the mid-converge marker the module docstring describes.
    """
    meta = dict(ns_manifest.get("metadata") or {})
    annotations = {
        k: v for k, v in (meta.get("annotations") or {}).items() if k != ANNOTATION_TEMPLATE_HASH
    }
    meta["annotations"] = annotations
    return {**ns_manifest, "metadata": meta}


def _prune(cluster: Cluster, namespace: str, keep: set[tuple[str, str]]) -> None:
    """Delete managed objects the current template set no longer renders.

    Label-scoped twice over: only kinds a template set may contain, and only
    objects carrying the provisioner's managed-by label - the API's workload
    Secrets and a tenant's own objects are invisible to it.
    """
    for kind in PRUNABLE_KINDS:
        existing = cluster.get(kind, label_selector=PROVISIONER_SELECTOR, namespace=namespace)
        for obj in existing:
            name = (obj.get("metadata") or {}).get("name", "")
            if (kind.kind, name) in keep:
                continue
            logger.info(
                "pruning %s '%s' from namespace '%s' (no longer in the template set)",
                kind.kind,
                name,
                namespace,
            )
            try:
                cluster.delete(kind, name, namespace=namespace)
            except NotFoundError:
                pass  # already gone between the list and the delete
