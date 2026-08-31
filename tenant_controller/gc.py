"""Namespace GC: collect tenant namespaces that have stayed empty of workloads.

A group's namespace is created on demand, but nothing else ever removes one: a
group that deletes its last workload (or stops using a region - a workload with
``"regions": ["central"]`` leaves the peer's namespace legitimately empty)
would otherwise hold its namespace, policies and credentials forever.

Slow and loud, modeled on the build controller's ``TagGC``:

- **A grace period, not a watch.** Immediacy is an anti-feature for namespace
  deletion. The first sweep that finds a namespace empty of Knative Services
  stamps *when*; only a namespace continuously empty past the grace period is
  deleted, and a workload appearing in between clears the stamp.
- **Local-only**, like the reconcile loop it rides in: each controller
  collects in its own cluster, and provisioning re-creates a collected
  namespace on the group's next deploy - so a mistaken collection costs one
  provision, never data.
- **Loud when off.** Deletion is the operator's call (``gc.enabled``, the
  ``registry.deleteOnFunctionDelete`` precedent), and a disabled GC says so
  rather than leaving silence to be read as health.

The stamps are annotations on the namespace (:mod:`common.labels`), written by
merge patch rather than the converge's server-side apply: the converge declares
only the template hash under its field manager, so its re-applies can never
erase a stamp this sweep wrote.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

from cloudlet_apis.logging import get_logger

from common.cluster import Cluster, ResourceKind
from common.errors import NotFoundError
from common.labels import ANNOTATION_EMPTY_SINCE, ANNOTATION_KEEP
from tenant_controller.config import TenantControllerSettings
from tenant_controller.reconcile import TENANT_CONTROLLER_SELECTOR

logger = get_logger(__name__)


class NamespaceGC:
    """The periodic sweep over this cluster's managed namespaces.

    The sweep runs on its own daemon thread, like ``TagGC``: it is apiserver
    I/O that must never sit inside the reconcile loop's pass, where every
    minute spent is a minute no template change rolls out.
    """

    def __init__(self, settings: TenantControllerSettings, cluster: Cluster):
        """Hold the knobs and say, audibly, whether this GC will delete.

        Args:
            settings: The GC knobs (enabled, interval, grace).
            cluster: The local cluster - the only one this GC touches.
        """
        self._cluster = cluster
        self._enabled = settings.gc_enabled
        self._interval = settings.gc_interval_seconds
        self._grace = settings.gc_grace_seconds
        # Zero: the first pass sweeps immediately.
        self._next_sweep = 0.0
        self._thread: threading.Thread | None = None
        if not self._enabled:
            logger.info(
                "namespace GC off: disabled by configuration (tenantNamespaces.gc.enabled); "
                "empty tenant namespaces are stamped but never deleted"
            )
        else:
            logger.info(
                "namespace GC on: deleting namespaces in %s empty for over %ds, sweeping every %ds",
                cluster.region,
                self._grace,
                self._interval,
            )

    def maybe_sweep(self) -> None:
        """Start a sweep when due; never raise into the reconcile loop.

        The next deadline is set when a sweep *starts*, so a failing apiserver
        retries at the next due pass rather than every pass. Stamping runs
        even with deletion disabled - the record of "empty since" is what lets
        an operator enable GC later without restarting the clock.
        """
        now = time.monotonic()
        if now < self._next_sweep:
            return
        self._next_sweep = now + self._interval
        if self._thread is not None and self._thread.is_alive():
            # A second sweep would race the first over the same namespaces.
            logger.warning(
                "namespace GC: previous sweep in '%s' still running after %ds; "
                "not starting another",
                self._cluster.region,
                self._interval,
            )
            return
        self._thread = threading.Thread(target=self._run, name="namespace-gc", daemon=True)
        self._thread.start()

    def wait(self, timeout: float | None = None) -> None:
        """Block until the running sweep (if any) finishes - for tests."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self) -> None:
        """The thread body: one sweep, with the loop's no-raise guarantee."""
        try:
            self.sweep()
        except Exception:  # noqa: BLE001 - a failed sweep is logged, not the loop's end
            logger.exception(
                "namespace GC: sweep failed in '%s'; retrying in ~%ds",
                self._cluster.region,
                self._interval,
            )

    def sweep(self) -> None:
        """One pass over every managed namespace in the local cluster.

        One namespace failing is logged and skipped, never the end of the
        sweep: the listing order is stable, so an aborting error would starve
        every namespace after the failing one, deterministically.
        """
        started = time.monotonic()
        namespaces = self._cluster.get(
            ResourceKind.NAMESPACE, label_selector=TENANT_CONTROLLER_SELECTOR, namespace=None
        )
        seen = deleted = failed = 0
        for ns in namespaces:
            seen += 1
            name = (ns.get("metadata") or {}).get("name", "")
            try:
                deleted += self._sweep_one(ns, name)
            except Exception:  # noqa: BLE001 - the next namespace still gets its sweep
                failed += 1
                logger.exception(
                    "namespace GC: sweeping '%s' failed; continuing with the rest", name
                )
        logger.info(
            "namespace GC: swept %d managed namespace(s) in '%s', deleted %d, %d failed, in %.1fs",
            seen,
            self._cluster.region,
            deleted,
            failed,
            time.monotonic() - started,
        )

    def _sweep_one(self, ns: dict, name: str) -> int:
        """Stamp, clear, or collect one namespace.

        Args:
            ns: The Namespace object, as the sweep listed it.
            name: Its name, for addressing and logs.

        Returns:
            1 if the namespace was deleted, else 0.
        """
        meta = ns.get("metadata") or {}
        if meta.get("deletionTimestamp"):
            return 0  # already on its way out
        annotations = meta.get("annotations") or {}

        # Empty means no Knative Services: everything else in the namespace
        # exists for one, or for the template set the delete may take with it.
        workloads = self._cluster.get(ResourceKind.KNATIVE_SERVICE, namespace=name)
        if workloads:
            if ANNOTATION_EMPTY_SINCE in annotations:
                self._annotate(name, {ANNOTATION_EMPTY_SINCE: None})
                logger.info("namespace GC: '%s' has workloads again; stamp cleared", name)
            return 0

        stamp = annotations.get(ANNOTATION_EMPTY_SINCE)
        empty_since = _parse(stamp)
        if empty_since is None:
            # First seen empty (or an unreadable stamp, which must not count
            # toward the grace): the clock starts now.
            if stamp is not None:
                logger.warning(
                    "namespace GC: '%s' carries an unreadable %s (%r); re-stamping",
                    name,
                    ANNOTATION_EMPTY_SINCE,
                    stamp,
                )
            self._annotate(name, {ANNOTATION_EMPTY_SINCE: _now().isoformat()})
            return 0

        age = (_now() - empty_since).total_seconds()
        if age <= self._grace:
            return 0
        if ANNOTATION_KEEP in annotations:
            logger.info(
                "namespace GC: '%s' empty for %.0fs but annotated %s; kept",
                name,
                age,
                ANNOTATION_KEEP,
            )
            return 0
        if not self._enabled:
            logger.info(
                "namespace GC: '%s' empty past the %ds grace; deletion is disabled, kept",
                name,
                self._grace,
            )
            return 0
        logger.info(
            "namespace GC: deleting '%s' (empty since %s, grace %ds)", name, stamp, self._grace
        )
        try:
            self._cluster.delete(ResourceKind.NAMESPACE, name, namespace=None)
        except NotFoundError:
            return 0  # gone between the list and the delete
        return 1

    def _annotate(self, name: str, annotations: dict[str, str | None]) -> None:
        """Merge-patch the namespace's annotations (None deletes a key)."""
        try:
            self._cluster.patch(
                ResourceKind.NAMESPACE,
                name,
                {"metadata": {"annotations": annotations}},
                namespace=None,
            )
        except NotFoundError:
            pass  # deleted underneath the sweep; nothing to record


def _now() -> datetime:
    """The sweep's clock, patchable in tests."""
    return datetime.now(UTC)


def _parse(stamp: str | None) -> datetime | None:
    """The annotation's RFC 3339 timestamp, or None when absent or unreadable."""
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
