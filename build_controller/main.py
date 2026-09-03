"""Build controller entrypoint: run the reconcile loop until the pod is stopped."""

from __future__ import annotations

from cloudlet_apis.logging import configure_logging, get_logger

from build_controller.config import BuildControllerSettings, get_settings
from build_controller.gc import TagGC
from build_controller.reconciler import Reconciler
from common.loop import install_terminate_handlers, run_loop

logger = get_logger(__name__)


def loop(reconciler: Reconciler, settings: BuildControllerSettings) -> None:
    """Resync and follow, forever (paced by ``common.loop``).

    Each pass resyncs and then holds the watch open for ``resync_seconds``, so
    no ``interval_seconds`` is passed to :func:`run_loop`; its minimum pass
    period is the only floor on a stream that closes immediately.

    Args:
        reconciler: The loop's work.
        settings: Pacing (resync interval, error backoff).
    """
    run_loop(
        lambda: reconciler.follow(settings.resync_seconds),
        error_backoff_seconds=settings.error_backoff_seconds,
    )


def run() -> None:
    """Configure logging and signals, then run the loop until terminated."""
    configure_logging()
    settings = get_settings()
    install_terminate_handlers()

    reconciler = Reconciler(settings, gc_factory=lambda region: TagGC(settings, region))
    logger.info("build controller watching kpack Images in %s", reconciler.local.region)
    try:
        loop(reconciler, settings)
    finally:
        reconciler.close()


if __name__ == "__main__":
    run()
