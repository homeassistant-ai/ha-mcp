"""Unit tests for the e2e fail-fast ``DoomedRunDetector``.

Covers the abort threshold, the reset-on-real-pass/fail semantics, and the
rerun/skip exclusions — the branching the e2e hook relies on (which is otherwise
only exercised incidentally on real e2e runs).
"""

from tests.src.doomed_run import DoomedRunDetector


def test_call_pass_resets_streak():
    d = DoomedRunDetector(threshold=5)
    for _ in range(4):
        assert d.record("setup", "failed") is False
    assert d.streak == 4
    assert d.record("call", "passed") is False
    assert d.streak == 0


def test_call_fail_resets_streak():
    d = DoomedRunDetector(threshold=5)
    for _ in range(4):
        d.record("setup", "failed")
    assert d.record("call", "failed") is False
    assert d.streak == 0


def test_rerun_outcome_neither_resets_nor_increments():
    d = DoomedRunDetector(threshold=5)
    d.record("setup", "failed")
    d.record("setup", "failed")
    assert d.streak == 2
    assert d.record("setup", "rerun") is False  # pytest-rerunfailures
    assert d.record("call", "rerun") is False
    assert d.streak == 2


def test_aborts_exactly_at_threshold():
    d = DoomedRunDetector(threshold=50)
    for _ in range(49):
        assert d.record("setup", "failed") is False
    assert d.record("setup", "failed") is True
    assert d.streak == 50


def test_interleaved_pass_prevents_abort():
    d = DoomedRunDetector(threshold=5)
    for _ in range(10):
        for _ in range(4):
            assert d.record("setup", "failed") is False
        assert d.record("call", "passed") is False
    assert d.streak == 0


def test_teardown_error_counts_toward_streak():
    d = DoomedRunDetector(threshold=2)
    assert d.record("teardown", "failed") is False
    assert d.record("teardown", "failed") is True


def test_skip_and_passing_setup_do_not_increment():
    d = DoomedRunDetector(threshold=2)
    assert d.record("setup", "skipped") is False
    assert d.record("setup", "passed") is False
    assert d.streak == 0


def test_default_threshold_is_50():
    assert DoomedRunDetector().threshold == 50
