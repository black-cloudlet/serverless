"""Build controller entrypoint: run the reconcile loop until the pod is stopped."""

from __future__ import annotations

from cloudlet_apis.logging import configure_logging, get_logger

from common.loop import install_terminate_handlers, run_loop
from controller.config import ControllerSettings, get_settings
from controller.gc import TagGC
from controller.reconciler import Reconciler

logger = get_logger(__name__)


def loop(reconciler: Reconciler, settings: ControllerSettings) -> None:
    """Resync and follow, forever (paced by ``common.loop``).

    The watch holds its own interval open, so no interval is passed - only
    the floor keeps a stream closed at the door from becoming back-to-back
    relists.

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
