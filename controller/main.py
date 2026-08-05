"""Build controller entrypoint: run the reconcile loop until the pod is stopped."""

from __future__ import annotations

import signal
import time

from common.logging import configure_logging, get_logger
from controller.config import ControllerSettings, get_settings
from controller.reconciler import Reconciler

logger = get_logger(__name__)


def _terminate(signum: int, _frame) -> None:
    """Turn SIGTERM/SIGINT into a SystemExit the blocking watch can unwind on.

    Raising is what makes it prompt. A handler that only set a flag would not be
    noticed until the next event or the watch timeout, which is minutes away and
    past the pod's grace period.
    """
    logger.info("received signal %s, shutting down", signum)
    raise SystemExit(0)


def loop(reconciler: Reconciler, settings: ControllerSettings) -> None:
    """Resync and follow, forever.

    A watch that ends is the normal case (its timeout expired) and leads
    straight into the next resync. A resync that *fails* is not, so it backs off
    - otherwise an unreachable cluster would spin.

    Args:
        reconciler: The loop's work.
        settings: Pacing (resync interval, error backoff).
    """
    while True:
        try:
            reconciler.follow(settings.resync_seconds)
        except Exception:  # noqa: BLE001 - the loop outlives any one pass
            logger.exception("reconcile pass failed, retrying")
            time.sleep(settings.error_backoff_seconds)


def run() -> None:
    """Configure logging and signals, then run the loop until terminated."""
    configure_logging()
    settings = get_settings()
    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)

    reconciler = Reconciler(settings)
    logger.info(
        "build controller watching kpack Images in %s, fanning out to %s",
        reconciler.local.site,
        ", ".join(settings.site_names),
    )
    try:
        loop(reconciler, settings)
    finally:
        reconciler.close()


if __name__ == "__main__":
    run()
