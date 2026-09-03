"""The paced control loop the long-running services share.

The build controller and the tenant controller both run their pass through
:func:`run_loop`, so pacing, error backoff and shutdown handling have one
implementation.
"""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Callable

from cloudlet_apis.logging import get_logger

logger = get_logger(__name__)

# The least a pass *period* may be, measured from one pass's start to the next.
# It applies to a pass that ends instantly (a watch closed at the door, an empty
# reconcile) as much as to a slow one.
MIN_PASS_SECONDS = 1.0

# Ceiling on the error backoff, which doubles while failures persist; a clean
# pass resets it to error_backoff_seconds.
MAX_BACKOFF_SECONDS = 60.0


def _terminate(signum: int, _frame) -> None:
    """Log the signal and raise SystemExit, unwinding a sleeping or blocking pass."""
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

    A raising pass is logged and sleeps ``error_backoff_seconds``, doubling up to
    ``MAX_BACKOFF_SECONDS`` while failures persist; the next clean pass resets
    the backoff.

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


class PeriodicSweep:
    """A slow background job offered a run each pass, pacing itself.

    The scaffolding the two GCs share, on the module's one-copy rule: the
    deadline is set when a sweep *starts* (a failing sweep retries at the next
    due pass, not every pass), the sweep runs on its own daemon thread (its
    I/O must never sit inside the loop's pass), a sweep outliving its interval
    blocks the next rather than racing it, and a raising sweep is logged, not
    the loop's end. Subclasses implement :meth:`sweep` and may veto a due run
    via :meth:`enabled` (silent - the operator turned it off) or
    :meth:`blocked` (the subclass logs why).
    """

    # Names this sweep in the shared log lines, e.g. "tag GC".
    label = "sweep"

    def __init__(self, interval: float, region: str, thread_name: str):
        """Set the pacing; the first offered run sweeps immediately.

        Args:
            interval: Seconds between sweep starts.
            region: The local region, for log lines.
            thread_name: The sweep thread's name.
        """
        self._interval = interval
        self._region = region
        self._thread_name = thread_name
        self._next_sweep = 0.0
        self._thread: threading.Thread | None = None

    def enabled(self) -> bool:
        """Whether this sweep runs at all; False is silent."""
        return True

    def blocked(self) -> str | None:
        """Why a due sweep must not run, or None; logged here, per interval."""
        return None

    def maybe_sweep(self, *args) -> None:
        """Start a sweep when due; never raise into the caller's loop.

        Args:
            *args: Passed through to :meth:`sweep`.
        """
        if not self.enabled():
            return
        now = time.monotonic()
        if now < self._next_sweep:
            return
        self._next_sweep = now + self._interval
        reason = self.blocked()
        if reason:
            logger.warning("%s off: %s", self.label, reason)
            return
        if self._thread is not None and self._thread.is_alive():
            logger.warning(
                "%s: previous sweep in '%s' still running after %ds; not starting another",
                self.label,
                self._region,
                self._interval,
            )
            return
        self._thread = threading.Thread(
            target=self._run, args=args, name=self._thread_name, daemon=True
        )
        self._thread.start()

    def wait(self, timeout: float | None = None) -> None:
        """Block until the running sweep (if any) finishes - for tests."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self, *args) -> None:
        """The thread body: one sweep, contained."""
        try:
            self.sweep(*args)
        except Exception:  # noqa: BLE001 - a failed sweep is logged, not the loop's end
            logger.exception(
                "%s: sweep failed in '%s'; retrying in ~%ds",
                self.label,
                self._region,
                self._interval,
            )

    def sweep(self, *args) -> None:
        """One sweep; subclasses implement it."""
        raise NotImplementedError
