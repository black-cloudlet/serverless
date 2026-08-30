"""Tenant controller entrypoint: the reconcile loop, with the provision API beside it.

Two jobs, one process: the level-triggered loop that converges this cluster,
and the HTTP call the API makes before a workload deploys. They share the
cluster clients and the mounted template set, which is the whole reason they
are not two deployments.
"""

from __future__ import annotations

import threading
import time

import uvicorn
from cloudlet_apis.logging import configure_logging, get_logger

from common.cluster import Cluster, clusters_for, select_local
from common.loop import install_terminate_handlers, run_loop
from tenant_controller.api import create_app
from tenant_controller.config import TenantControllerSettings, get_settings
from tenant_controller.reconcile import reconcile_all
from tenant_controller.templates import TemplateSet

logger = get_logger(__name__)

# How long uvicorn lets in-flight requests finish once asked to stop.
API_SHUTDOWN_SECONDS = 5.0
# How long the loop then waits for that thread. Deliberately longer than the
# budget above: the join must normally outlast uvicorn's own graceful
# shutdown, or it expires just as the server is finishing and the caller
# closes the cluster clients out from under an in-flight converge.
API_JOIN_SECONDS = 15.0
# A server that cannot bind must not be discovered by the first provision.
API_STARTUP_SECONDS = 10.0


def run_pass(cluster: Cluster, settings: TenantControllerSettings, *, force: bool = False) -> None:
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


def loop(cluster: Cluster, settings: TenantControllerSettings) -> None:
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
    settings: TenantControllerSettings, clusters: list[Cluster]
) -> tuple[uvicorn.Server, threading.Thread]:
    """Start the provision API on a background thread.

    Background, so the loop keeps the main thread: uvicorn installs no signal
    handlers off it (it checks), leaving SIGTERM to unwind the loop as before.
    Daemon, so a server that will not stop cannot hold the pod past its grace.

    Args:
        settings: For the listen port.
        clusters: Every region's cluster - provisioning fans out to all of them.

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
    thread = threading.Thread(target=server.run, name="provision-api", daemon=True)
    thread.start()
    _await_startup(server, thread)
    return server, thread


def _await_startup(server: uvicorn.Server, thread: threading.Thread) -> None:
    """Block until the server is listening, or say why it never will be.

    uvicorn answers a bind failure with ``sys.exit`` *inside this thread*,
    which Python discards silently - so without this the loop would run on
    beside a dead API and every provision would be refused with nothing in the
    log to say so. Raising instead crash-loops the pod, which is visible.

    Raises:
        RuntimeError: If the server stopped, or did not start in time.
    """
    deadline = time.monotonic() + API_STARTUP_SECONDS
    while time.monotonic() < deadline:
        if server.started:
            return
        if not thread.is_alive():
            raise RuntimeError("the provision API stopped before it began serving")
        time.sleep(0.05)
    raise RuntimeError(f"the provision API did not start within {API_STARTUP_SECONDS}s")


def stop(server: uvicorn.Server, thread: threading.Thread) -> None:
    """Ask the server to stop and wait for it, so shutdown stays ordered.

    The wait matters: the server's own shutdown drains the provision pool, and
    only once that returns may the caller close the cluster clients those
    converges write through.
    """
    server.should_exit = True
    thread.join(timeout=API_JOIN_SECONDS)
    if thread.is_alive():
        logger.warning(
            "the provision API did not stop within %ss; closing cluster clients anyway",
            API_JOIN_SECONDS,
        )


def run() -> None:
    """Configure logging and signals, then run the loop until terminated."""
    configure_logging()
    settings = get_settings()
    install_terminate_handlers()

    # Every region: provisioning writes to all of them. The loop below still takes
    # only the local one - converging a peer cluster from here is what the
    # local-only rule exists to prevent.
    clusters = clusters_for(settings)
    local = select_local(clusters, settings.local_region)
    logger.info(
        "tenant controller reconciling namespaces in %s from %s, provision API on :%d",
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
