"""Converging tenant namespaces to the template set, one cluster at a time.

Local-only, like the build controller: each provisioner converges its own
cluster from its own cluster's ConfigMap, so the two sites never fight during
Argo sync skew. The stamp protocol makes a converge crash-safe:

1. Apply the Namespace *without* the hash annotation (under SSA this removes
   the old stamp, marking the converge in progress).
2. Apply the contents, then prune managed objects the set no longer renders.
3. Re-apply the Namespace *with* the hash - last, so a crash anywhere above
   leaves no stamp and the next pass redoes the namespace.
"""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor

from cloudlet_apis.logging import get_logger

from common.cluster import Cluster, ResourceKind
from common.errors import NotFoundError
from common.labels import (
    ANNOTATION_TEMPLATE_HASH,
    LABEL_GROUP,
    LABEL_MANAGED_BY,
    PROVISIONER_VALUE,
)
from provisioner.templates import TEMPLATE_KINDS, TemplateSet

logger = get_logger(__name__)

# The SSA identity every provisioner write carries: what lets a re-apply
# remove fields it no longer declares without touching anyone else's.
FIELD_MANAGER = "serverless-provisioner"

# What the provisioner manages, and nothing else.
PROVISIONER_SELECTOR = f"{LABEL_MANAGED_BY}={PROVISIONER_VALUE}"

# What the prune sweeps: the same vocabulary the render gate admits, so a set
# can never create what the prune could not collect - and a kind removed from
# the set entirely still has its leftovers swept.
PRUNABLE_KINDS = TEMPLATE_KINDS


def converge(
    cluster: Cluster,
    namespace: str,
    group: str,
    templates: TemplateSet,
    *,
    clear_stamp: bool = True,
    managed: Mapping[str, set[tuple[str, str]]] | None = None,
) -> None:
    """Bring one tenant namespace to the template set (the stamp protocol above).

    Idempotent throughout, so the ensure call and the loop can both run it.

    Args:
        cluster: The cluster to converge in (cluster-scoped client).
        namespace: The namespace's name - passed, not derived, so a changed
            suffix setting cannot strand existing namespaces.
        group: The owning (normalized) group.
        templates: The loaded template set.
        clear_stamp: Apply the opening hash-less Namespace. False only when
            the caller observed no stamp AND the namespace already exists (a
            listed-but-never-converged namespace): there is nothing to clear,
            and the crash-safety property - no stamp until the set landed -
            already holds, so the write is saved.
        managed: A pre-listed ``{namespace: {(kind, name)}}`` snapshot of the
            provisioner's objects, so a pass prunes from one listing per kind
            instead of one per kind per namespace. None lists here.

    Raises:
        Exception: Any render or apply error; the caller decides whether it
            ends the pass.
    """
    manifests = templates.render(namespace=namespace, group=group)
    ns_manifest = next((m for m in manifests if m["kind"] == "Namespace"), None)
    if ns_manifest is None:
        ns_manifest = {"apiVersion": "v1", "kind": "Namespace", "metadata": {}}
    contents = [m for m in manifests if m["kind"] != "Namespace"]
    if not contents:
        # Files that render to nothing read as a truncated ConfigMap, and
        # obeying them would prune the namespace bare. Raised before any
        # write, so the stamp survives and the next pass retries.
        raise ValueError(f"template set {templates.digest} rendered no namespaced contents")

    # The target's name wins over the template's: a template naming something
    # else would create a second namespace mid-converge.
    ns_manifest = _stamped(ns_manifest, group)
    ns_manifest["metadata"]["name"] = namespace

    # 1. Namespace first, without the hash: mid-converge marker.
    if clear_stamp:
        cluster.apply(_without_hash(ns_manifest), namespace=None, field_manager=FIELD_MANAGER)

    # 2. Contents, then prune.
    keep: set[tuple[str, str]] = set()
    for manifest in contents:
        stamped = _stamped(manifest, group)
        # The target's namespace wins over the template's, as its name does
        # for the Namespace above.
        stamped["metadata"]["namespace"] = namespace
        keep.add((manifest["kind"], stamped["metadata"]["name"]))
        cluster.apply(stamped, namespace=namespace, field_manager=FIELD_MANAGER)
    _prune(
        cluster,
        namespace,
        keep,
        existing=None if managed is None else managed.get(namespace, set()),
    )

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


def reconcile_all(
    cluster: Cluster, templates: TemplateSet, *, force: bool = False, workers: int = 1
) -> tuple[int, int, int]:
    """Converge every managed namespace in this cluster whose stamp is stale.

    One namespace failing is logged and skipped, never the end of the pass
    (``TagGC``'s rule: an aborting error would starve every namespace after
    it, deterministically). An empty set is refused: mounted-but-empty is
    indistinguishable from a broken mount, and obeying it would prune every
    tenant namespace bare.

    Args:
        cluster: The local cluster (cluster-scoped client).
        templates: The currently mounted template set.
        force: Converge even stamp-matching namespaces - the periodic drift
            repair, since a deleted object does not change the stamp.
        workers: Converges run on a thread pool of this size - independent
            per namespace, so a template rollout over many tenants is bounded
            by the pool, not serialized. 1 runs them inline.

    Returns:
        ``(seen, converged, failed)``, for the log line and the pass verdict.
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
    seen = failed = 0
    stale: list[tuple[str, str, str | None]] = []
    for ns in namespaces:
        seen += 1
        meta = ns.get("metadata") or {}
        name = meta.get("name", "")
        group = (meta.get("labels") or {}).get(LABEL_GROUP)
        if not group:
            # Ours by label but unattributable - a human made this state.
            logger.warning("managed namespace '%s' carries no group label; skipping", name)
            failed += 1
            continue
        stamp = (meta.get("annotations") or {}).get(ANNOTATION_TEMPLATE_HASH)
        if stamp == templates.digest and not force:
            continue
        stale.append((name, group, stamp))

    # One listing per kind for the whole pass, not per namespace. Taken
    # before the converges: anything they apply is in their keep sets, so a
    # pre-apply snapshot can never prune what a converge just wrote.
    managed = _managed_snapshot(cluster) if stale else {}

    def one(entry: tuple[str, str, str | None]) -> bool:
        name, group, stamp = entry
        try:
            converge(
                cluster,
                name,
                group,
                templates,
                clear_stamp=stamp is not None,
                managed=managed,
            )
            return True
        except Exception:  # noqa: BLE001 - the next namespace still gets its converge
            logger.exception("converging namespace '%s' failed; continuing", name)
            return False

    if len(stale) > 1 and workers > 1:
        with ThreadPoolExecutor(max_workers=min(workers, len(stale))) as pool:
            outcomes = list(pool.map(one, stale))
    else:
        outcomes = [one(entry) for entry in stale]
    converged = sum(outcomes)
    failed += len(outcomes) - converged
    logger.info(
        "reconciled %d managed namespace(s) in %s: %d converged, %d failed, set %s",
        seen,
        cluster.region,
        converged,
        failed,
        templates.digest,
    )
    return (seen, converged, failed)


def _managed_snapshot(cluster: Cluster) -> dict[str, set[tuple[str, str]]]:
    """Every provisioner-labeled prunable object, bucketed by namespace."""
    managed: dict[str, set[tuple[str, str]]] = {}
    for kind in PRUNABLE_KINDS:
        for obj in cluster.get(kind, label_selector=PROVISIONER_SELECTOR, namespace=None):
            meta = obj.get("metadata") or {}
            ns = meta.get("namespace", "")
            managed.setdefault(ns, set()).add((kind.kind, meta.get("name", "")))
    return managed


def _stamped(manifest: dict, group: str) -> dict:
    """A copy of ``manifest`` carrying the provisioner's ownership labels.

    Injected in code, not trusted to templates: the prune and the GC select
    on these labels.
    """
    meta = dict(manifest.get("metadata") or {})
    labels = dict(meta.get("labels") or {})
    labels[LABEL_MANAGED_BY] = PROVISIONER_VALUE
    labels[LABEL_GROUP] = group
    meta["labels"] = labels
    return {**manifest, "metadata": meta}


def _without_hash(ns_manifest: dict) -> dict:
    """The namespace manifest with no template-hash annotation declared."""
    meta = dict(ns_manifest.get("metadata") or {})
    annotations = {
        k: v for k, v in (meta.get("annotations") or {}).items() if k != ANNOTATION_TEMPLATE_HASH
    }
    meta["annotations"] = annotations
    return {**ns_manifest, "metadata": meta}


def _prune(
    cluster: Cluster,
    namespace: str,
    keep: set[tuple[str, str]],
    *,
    existing: set[tuple[str, str]] | None = None,
) -> None:
    """Delete managed objects the current template set no longer renders.

    Only prunable kinds, only provisioner-labeled objects: the API's workload
    Secrets and a tenant's own objects are invisible to it.

    Args:
        cluster: The cluster to prune in.
        namespace: The tenant namespace.
        keep: The (kind, name) pairs the current render produced.
        existing: This namespace's slice of a pass-wide snapshot
            (``_managed_snapshot``); None lists here, per kind.
    """
    if existing is None:
        existing = {
            (kind.kind, (obj.get("metadata") or {}).get("name", ""))
            for kind in PRUNABLE_KINDS
            for obj in cluster.get(kind, label_selector=PROVISIONER_SELECTOR, namespace=namespace)
        }
    by_kind = {k.kind: k for k in PRUNABLE_KINDS}
    for kind_name, name in sorted(existing - keep):
        logger.info(
            "pruning %s '%s' from namespace '%s' (no longer in the template set)",
            kind_name,
            name,
            namespace,
        )
        try:
            cluster.delete(by_kind[kind_name], name, namespace=namespace)
        except NotFoundError:
            pass  # already gone between the list and the delete
