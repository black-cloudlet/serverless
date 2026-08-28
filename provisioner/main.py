"""Provisioner entrypoint: run the reconcile loop until the pod is stopped."""

from __future__ import annotations

import signal
import time

from cloudlet_apis.logging import configure_logging, get_logger

from common.cluster import Cluster, clusters_for, select_local
from provisioner.config import ProvisionerSettings, get_settings
from provisioner.reconcile import reconcile_all
from provisioner.templates import TemplateSet

logger = get_logger(__name__)


def _terminate(signum: int, _frame) -> None:
    """Raise, so a sleeping loop unwinds now rather than at the next pass."""
    logger.info("received signal %s, shutting down", signum)
    raise SystemExit(0)


# The least a pass may take, so the loop can never degenerate into
# back-to-back LISTs of every namespace at full speed.
_MIN_PASS_SECONDS = 1.0


def run_pass(cluster: Cluster, settings: ProvisionerSettings, *, force: bool = False) -> None:
    """One reconcile pass: load the mounted set fresh, converge the stale.

    Re-read every pass - the kubelet refreshes the mount in place, and that
    refresh is how a helm upgrade reaches existing namespaces.

    Args:
        cluster: The local cluster.
        settings: For the templates directory.
        force: Converge even stamp-matching namespaces (the drift repair).

    Raises:
        RuntimeError: If every managed namespace failed to converge - one
            cause, not many, so it takes the loop's backoff rather than a
            full resync sleep.
    """
    templates = TemplateSet.load(settings.templates_dir)
    seen, _converged, failed = reconcile_all(cluster, templates, force=force)
    if seen and failed == seen:
        raise RuntimeError(f"all {seen} managed namespace(s) failed to converge")


def loop(cluster: Cluster, settings: ProvisionerSettings) -> None:
    """Reconcile, sleep, forever.

    A raising pass backs off and retries; a clean pass waits the interval.
    Per-namespace failures never reach here - ``reconcile_all`` contains them.

    Args:
        cluster: The local cluster.
        settings: Pacing (resync interval, error backoff).
    """
    passes = 0
    while True:
        passes += 1
        started = time.monotonic()
        try:
            # Every Nth pass forces a full converge, so drift in the objects
            # themselves is repaired without waiting for a template change.
            run_pass(cluster, settings, force=passes % settings.full_resync_passes == 0)
        except Exception:  # noqa: BLE001 - the loop outlives any one pass
            logger.exception("reconcile pass failed, retrying")
            time.sleep(settings.error_backoff_seconds)
            continue
        elapsed = time.monotonic() - started
        time.sleep(max(settings.resync_seconds - elapsed, _MIN_PASS_SECONDS))


def run() -> None:
    """Configure logging and signals, then run the loop until terminated."""
    configure_logging()
    settings = get_settings()
    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)

    # Local-only, like the build controller.
    cluster = select_local(clusters_for(settings), settings.local_region)
    logger.info(
        "provisioner reconciling tenant namespaces in %s from %s",
        cluster.region,
        settings.templates_dir,
    )
    try:
        loop(cluster, settings)
    finally:
        cluster.close()


if __name__ == "__main__":
    run()
