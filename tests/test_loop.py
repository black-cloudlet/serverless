"""The shared paced loop: backoff on failure, interval or floor on success."""

from __future__ import annotations

import pytest

from common import loop as common_loop


def _run(passes, monkeypatch, **kwargs):
    """Drive run_loop through `passes` outcomes, capturing sleeps."""
    outcomes = iter(passes)
    sleeps = []

    def one_pass():
        outcome = next(outcomes)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(common_loop.time, "sleep", sleeps.append)
    with pytest.raises(SystemExit):
        common_loop.run_loop(one_pass, **kwargs)
    return sleeps


def test_a_raising_pass_backs_off_and_retries(monkeypatch):
    sleeps = _run(
        [RuntimeError("down"), None, SystemExit(0)],
        monkeypatch,
        error_backoff_seconds=2.5,
        interval_seconds=300,
    )
    assert sleeps[0] == 2.5  # the raise took the backoff...
    assert sleeps[1] == pytest.approx(300, abs=2)  # ...the clean pass, the interval


def test_a_pass_holding_its_own_interval_is_only_floored(monkeypatch):
    # The controller's watch: the pass lasts the interval itself, so a pass
    # that ends instantly (a stream closed at the door) is floored, not
    # slept a second interval.
    sleeps = _run(
        [None, SystemExit(0)],
        monkeypatch,
        error_backoff_seconds=5.0,
        interval_seconds=None,
    )
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= common_loop.MIN_PASS_SECONDS


def test_the_interval_never_paces_below_the_floor(monkeypatch):
    # Even a pass that overran its interval yields the floor, so the loop can
    # never degenerate into back-to-back relists at full speed.
    sleeps = _run(
        [None, SystemExit(0)],
        monkeypatch,
        error_backoff_seconds=5.0,
        interval_seconds=0,
    )
    assert sleeps[0] == common_loop.MIN_PASS_SECONDS


def test_terminate_raises_system_exit():
    with pytest.raises(SystemExit):
        common_loop._terminate(15, None)
