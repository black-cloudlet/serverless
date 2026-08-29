"""Provisioner entrypoint: the reconcile loop, with the ensure API beside it.

Two jobs, one process: the level-triggered loop that converges this cluster,
and the HTTP call the API makes before a workload deploys. They share the
cluster clients and the mounted template set, which is the whole reason they
are not two deployments.
"""

from __future__ import annotations

import threading

import uvicorn
from cloudlet_apis.logging import configure_logging, get_logger

from common.cluster import Cluster, clusters_for, select_local
from common.loop import install_terminate_handlers, run_loop
from provisioner.api import create_app
from provisioner.config import ProvisionerSettings, get_settings
from provisioner.reconcile import reconcile_all
from provisioner.templates import TemplateSet

logger = get_logger(__name__)

# How long a stopping pod lets an in-flight ensure finish. Short: the caller
# retries, and the pod's own grace period is the ceiling either way.
API_SHUTDOWN_SECONDS = 5.0


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


def serve(
    settings: ProvisionerSettings, clusters: list[Cluster]
) -> tuple[uvicorn.Server, threading.Thread]:
    """Start the ensure API on a background thread.

    Background, so the loop keeps the main thread: uvicorn installs no signal
    handlers off it (it checks), leaving SIGTERM to unwind the loop as before.
    Daemon, so a server that will not stop cannot hold the pod past its grace.

    Args:
        settings: For the listen port.
        clusters: Every region's cluster - ensure fans out to all of them.

    Returns:
        The server and the thread running it, for :func:`stop`.
    """
    config = uvicorn.Config(
        create_app(clusters, settings),
        host="0.0.0.0",  # noqa: S104
        port=settings.port,
        # Ours is already configured; uvicorn's dictConfig would replace it.
        log_config=None,
        timeout_graceful_shutdown=int(API_SHUTDOWN_SECONDS),
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="ensure-api", daemon=True)
    thread.start()
    return server, thread


def stop(server: uvicorn.Server, thread: threading.Thread) -> None:
    """Let an in-flight ensure finish, then stop waiting for the server."""
    server.should_exit = True
    thread.join(timeout=API_SHUTDOWN_SECONDS)


def run() -> None:
    """Configure logging and signals, then run the loop until terminated."""
    configure_logging()
    settings = get_settings()
    install_terminate_handlers()

    # Every region: ensure writes to all of them. The loop below still takes
    # only the local one - converging a peer cluster from here is what the
    # local-only rule exists to prevent.
    clusters = clusters_for(settings)
    local = select_local(clusters, settings.local_region)
    logger.info(
        "provisioner reconciling tenant namespaces in %s from %s, ensure API on :%d",
        local.region,
        settings.templates_dir,
        settings.port,
    )
    server, thread = serve(settings, list(clusters.values()))
    try:
        loop(local, settings)
    finally:
        stop(server, thread)
        for cluster in clusters.values():
            cluster.close()


if __name__ == "__main__":
    run()
