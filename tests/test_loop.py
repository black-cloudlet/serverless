"""The shared paced loop: capped backoff on failure, one pass period on success."""

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


def test_a_raising_pass_backs_off_then_paces_the_clean_one(monkeypatch):
    sleeps = _run(
        [RuntimeError("down"), None, SystemExit(0)],
        monkeypatch,
        error_backoff_seconds=2.5,
        interval_seconds=300,
    )
    assert sleeps[0] == 2.5  # the raise took the backoff...
    assert sleeps[1] == pytest.approx(300, abs=2)  # ...the clean pass, the period


def test_backoff_doubles_while_failures_persist_and_resets_on_success(monkeypatch):
    # A sustained outage must not retry a full pass every 5s at an apiserver
    # that is already struggling; a recovery returns to the prompt retry.
    sleeps = _run(
        [RuntimeError("down")] * 3 + [None, RuntimeError("down"), SystemExit(0)],
        monkeypatch,
        error_backoff_seconds=5.0,
        interval_seconds=300,
    )
    assert sleeps[:3] == [5.0, 10.0, 20.0]
    assert sleeps[4] == 5.0  # the clean pass reset it


def test_backoff_is_capped(monkeypatch):
    sleeps = _run(
        [RuntimeError("down")] * 8 + [SystemExit(0)],
        monkeypatch,
        error_backoff_seconds=5.0,
    )
    assert max(sleeps) == common_loop.MAX_BACKOFF_SECONDS


def test_a_pass_holding_its_own_interval_gets_only_the_floor(monkeypatch):
    # The controller's watch lasts the interval itself, so it passes none: a
    # pass that ends instantly is floored, not slept a second interval.
    sleeps = _run([None, SystemExit(0)], monkeypatch, error_backoff_seconds=5.0)
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= common_loop.MIN_PASS_SECONDS


def test_no_interval_and_a_zero_interval_mean_the_same_thing(monkeypatch):
    # One rule, so there is no mode to get wrong: 0 is not a special value.
    default = _run([None, SystemExit(0)], monkeypatch, error_backoff_seconds=5.0)
    explicit = _run(
        [None, SystemExit(0)], monkeypatch, error_backoff_seconds=5.0, interval_seconds=0
    )
    assert default[0] == pytest.approx(explicit[0], abs=0.1)


def test_a_pass_that_overran_its_period_starts_again_at_once(monkeypatch):
    # The period is measured from the pass's start, so an overrunning pass
    # has already paid it - no extra sleep on top.
    ticks = iter([0.0, 500.0, 500.0, 500.0])
    monkeypatch.setattr(common_loop.time, "monotonic", lambda: next(ticks))
    sleeps = _run(
        [None, SystemExit(0)], monkeypatch, error_backoff_seconds=5.0, interval_seconds=300
    )
    assert sleeps == [0.0]


def test_terminate_raises_system_exit():
    with pytest.raises(SystemExit):
        common_loop._terminate(15, None)
