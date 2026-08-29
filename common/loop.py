"""The paced control loop the long-running services share.

One pacing/backoff policy for the build controller and the tenant controller, so a
hardening fix (jitter, a backoff cap, shutdown handling) lands once instead of
in whichever copy the incident pointed at.
"""

from __future__ import annotations

import signal
import time
from collections.abc import Callable

from cloudlet_apis.logging import get_logger

logger = get_logger(__name__)

# The least a pass *period* may be, measured from one pass's start to the
# next: a pass that ends instantly (a watch closed at the door, an empty
# reconcile) cannot degenerate into back-to-back LISTs at full speed.
MIN_PASS_SECONDS = 1.0

# A sustained outage retries at a doubling interval rather than hammering an
# apiserver that is already struggling; a clean pass resets it.
MAX_BACKOFF_SECONDS = 60.0


def _terminate(signum: int, _frame) -> None:
    """Raise, so a sleeping or blocking pass unwinds now rather than later."""
    logger.info("received signal %s, shutting down", signum)
    raise SystemExit(0)


def install_terminate_handlers() -> None:
    """Make SIGTERM/SIGINT raise SystemExit in the loop's thread."""
    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)


def run_loop(
    run_pass: Callable[[], None],
    *,
    error_backoff_seconds: float,
    interval_seconds: float = 0.0,
) -> None:
    """Run ``run_pass`` forever: back off on failure, pace success.

    One pacing rule: a pass starts no sooner than ``interval_seconds`` after
    the previous one started, and never sooner than ``MIN_PASS_SECONDS``. A
    pass that holds its own interval open (the controller's watch) passes no
    interval and gets only the floor; one that returns immediately (the
    tenant controller's reconcile) passes its resync interval. Either way a pass
    that overran its period simply starts again.

    A raising pass sleeps ``error_backoff_seconds``, doubling up to
    ``MAX_BACKOFF_SECONDS`` while failures persist, so a transient failure
    retries promptly and a sustained outage does not hammer the cluster.

    Args:
        run_pass: One pass of the service's work.
        error_backoff_seconds: The sleep after the first failing pass.
        interval_seconds: The minimum pass period; 0 for the floor alone.
    """
    period = max(interval_seconds, MIN_PASS_SECONDS)
    backoff = error_backoff_seconds
    while True:
        started = time.monotonic()
        try:
            run_pass()
        except Exception:  # noqa: BLE001 - the loop outlives any one pass
            logger.exception("pass failed, retrying in %.1fs", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS) if backoff else 0.0
            continue
        backoff = error_backoff_seconds
        time.sleep(max(period - (time.monotonic() - started), 0.0))
