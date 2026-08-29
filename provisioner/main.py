"""Provisioner entrypoint: run the reconcile loop until the pod is stopped."""

from __future__ import annotations

from cloudlet_apis.logging import configure_logging, get_logger

from common.cluster import Cluster, clusters_for, select_local
from common.loop import install_terminate_handlers, run_loop
from provisioner.config import ProvisionerSettings, get_settings
from provisioner.reconcile import reconcile_all
from provisioner.templates import TemplateSet

logger = get_logger(__name__)


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
    seen, _converged, failed = reconcile_all(
        cluster, templates, force=force, workers=settings.converge_workers
    )
    if seen and failed == seen:
        raise RuntimeError(f"all {seen} managed namespace(s) failed to converge")


def loop(cluster: Cluster, settings: ProvisionerSettings) -> None:
    """Reconcile, sleep, forever (paced by ``common.loop``).

    Per-namespace failures never reach the pacing - ``reconcile_all``
    contains them; only an all-failed pass raises into the backoff.

    Args:
        cluster: The local cluster.
        settings: Pacing (resync interval, error backoff).
    """
    passes = 0

    def one_pass() -> None:
        nonlocal passes
        passes += 1
        # Every Nth pass forces a full converge, so drift in the objects
        # themselves is repaired without waiting for a template change.
        run_pass(cluster, settings, force=passes % settings.full_resync_passes == 0)

    run_loop(
        one_pass,
        error_backoff_seconds=settings.error_backoff_seconds,
        interval_seconds=settings.resync_seconds,
    )


def run() -> None:
    """Configure logging and signals, then run the loop until terminated."""
    configure_logging()
    settings = get_settings()
    install_terminate_handlers()

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
