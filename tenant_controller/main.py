"""Tenant controller entrypoint: the reconcile loop, with the provision API beside it.

Two jobs, one process: the level-triggered loop that converges this cluster,
and the HTTP call the API makes before a workload deploys. They share the
cluster clients and the mounted template set.
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
from tenant_controller.gc import NamespaceGC
from tenant_controller.reconcile import reconcile_all
from tenant_controller.templates import TemplateSet

logger = get_logger(__name__)

# How long uvicorn lets in-flight requests finish once asked to stop.
API_SHUTDOWN_SECONDS = 5.0
# How long the loop then waits for that thread. Longer than the budget above,
# so the join outlasts uvicorn's own graceful shutdown; the cluster clients are
# closed once it returns.
API_JOIN_SECONDS = 15.0
# How long `serve` waits for the server to report that it is listening.
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
        RuntimeError: If every managed namespace failed to converge; the loop
            then takes its error backoff instead of the resync interval.
    """
    templates = TemplateSet.load(settings.templates_dir)
    seen, _converged, failed = reconcile_all(
        cluster, templates, force=force, workers=settings.converge_workers
    )
    if seen and failed == seen:
        raise RuntimeError(f"all {seen} managed namespace(s) failed to converge")


def loop(cluster: Cluster, settings: TenantControllerSettings, gc: NamespaceGC) -> None:
    """Reconcile, sleep, forever (paced by ``common.loop``).

    Per-namespace failures never reach the pacing - ``reconcile_all``
    contains them; only an all-failed pass raises into the backoff.

    Args:
        cluster: The local cluster.
        settings: Pacing (resync interval, error backoff).
        gc: The namespace GC, offered a sweep each pass (it paces itself).
    """
    passes = 0

    def one_pass() -> None:
        nonlocal passes
        passes += 1
        # Every Nth pass forces a full converge, so drift in the objects
        # themselves is repaired without waiting for a template change.
        run_pass(cluster, settings, force=passes % settings.full_resync_passes == 0)
        # After the converge, on its own thread: never inside the pass's time.
        gc.maybe_sweep()

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
    handlers off the main thread (it checks), leaving SIGTERM to unwind the
    loop. Daemon, so a server that will not stop cannot hold the pod past its
    grace period. Blocks until the server is listening.

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

    uvicorn answers a bind failure with ``sys.exit`` *inside the server
    thread*, which Python discards silently, so a dead server shows up here as
    a thread that is no longer alive rather than as an exception. Polls
    ``server.started`` and the thread every 50ms until ``API_STARTUP_SECONDS``,
    then raises: a crash-looping pod is visible, where a loop running on beside
    a dead API and refusing every provision is not.

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

    The join is what orders shutdown: the server's own shutdown drains the
    provision pool, and only once it returns may the caller close the cluster
    clients those converges write through. A thread still alive after
    ``API_JOIN_SECONDS`` is logged and left; the caller closes anyway.
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

    # Every region: provisioning writes to all of them. The loop below takes
    # only the local one, per the local-only rule.
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
        loop(local, settings, NamespaceGC(settings, local))
    finally:
        stop(server, thread)
        for cluster in clusters.values():
            cluster.close()


if __name__ == "__main__":
    run()
