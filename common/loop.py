"""The paced control loop the long-running services share.

One pacing/backoff policy for the build controller and the provisioner, so a
hardening fix (jitter, a backoff cap, shutdown handling) lands once instead of
in whichever copy the incident pointed at.
"""

from __future__ import annotations

import signal
import time
from collections.abc import Callable

from cloudlet_apis.logging import get_logger

logger = get_logger(__name__)

# The least a pass may take before the next one starts, so a pass that ends
# instantly (a watch closed at the door, an empty reconcile) cannot degenerate
# into back-to-back LISTs at full speed.
MIN_PASS_SECONDS = 1.0


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
    interval_seconds: float | None = None,
) -> None:
    """Run ``run_pass`` forever: back off on failure, pace success.

    A raising pass sleeps ``error_backoff_seconds`` and retries, so a
    transient failure does not wait out a whole interval.

    Args:
        run_pass: One pass of the service's work.
        error_backoff_seconds: The sleep after a raising pass.
        interval_seconds: The pause a clean pass waits out (minus its own
            duration). None for a pass that holds its interval open itself -
            the controller's watch - which is then only floored to
            ``MIN_PASS_SECONDS``.
    """
    while True:
        started = time.monotonic()
        try:
            run_pass()
        except Exception:  # noqa: BLE001 - the loop outlives any one pass
            logger.exception("pass failed, retrying")
            time.sleep(error_backoff_seconds)
            continue
        elapsed = time.monotonic() - started
        if interval_seconds is None:
            if elapsed < MIN_PASS_SECONDS:
                time.sleep(MIN_PASS_SECONDS - elapsed)
        else:
            time.sleep(max(interval_seconds - elapsed, MIN_PASS_SECONDS))
