"""Doomed-run detector and the e2e fail-fast hook that drives it.

Kept out of ``tests/src/e2e/conftest.py`` so the hook is testable without
importing the heavy e2e conftest, which re-exports ``pytest_runtest_logreport``
to register it. Tests: ``tests/src/unit/test_doomed_run.py``.
"""

from __future__ import annotations

import pytest

DOOMED_RUN_ERROR_STREAK = 50


class DoomedRunDetector:
    """Counts CONSECUTIVE setup/teardown errors with zero call-phase pass/fail
    between them.

    ``record(when, outcome)`` returns ``True`` once the streak reaches
    ``threshold`` — the signal that the run is producing nothing but errors and
    should be aborted. A genuine call-phase ``passed``/``failed`` resets the
    streak, so an isolated flaky-setup test never trips it. pytest-rerunfailures'
    intermediate ``rerun`` outcome is ignored (neither resets nor increments), as
    is ``skipped`` and a non-failing setup/teardown.
    """

    def __init__(self, threshold: int = DOOMED_RUN_ERROR_STREAK) -> None:
        self.threshold = threshold
        self.streak = 0

    def record(self, when: str, outcome: str) -> bool:
        # A real test body ran -> the run is alive; reset.
        if when == "call" and outcome in ("passed", "failed"):
            self.streak = 0
            return False
        # A setup/teardown error (an "error", not a "fail"). ``== "failed"``
        # excludes the "rerun" outcome, which must not extend a doomed streak.
        if when in ("setup", "teardown") and outcome == "failed":
            self.streak += 1
            return self.streak >= self.threshold
        return False


# Fail fast on a doomed run, on EVERY e2e lane (the e2e conftest is shared by the
# testcontainer / external-HAOS / inaddon suites, so the hook guards all three).
# A Supervisor add-on-update flake did exactly this on PR #1699: all 997 inaddon
# tests ERRORed at setup (0 passed, 0 failed) while the run ground on 11m39s
# producing nothing but errors. DoomedRunDetector aborts once it sees 50
# consecutive setup/teardown errors with zero call-phase pass/fail between them;
# a genuine pass/fail resets the streak, so real failures still run through in
# full (this is NOT --maxfail).
#
# Under xdist, pytest_runtest_logreport fires in EACH process — the controller
# (which receives every worker's reports) AND each worker for its own tests — so
# every process keeps its own module-global detector; the streak is per-process,
# not global. That is fine: a doomed run errors on every process, so whichever
# reaches 50 first calls pytest.exit and ends the session (validated under -n2:
# a 150-test all-error run aborts at 50 in ~2s).
_doomed_detector = DoomedRunDetector()
_reported_failure_workers: set[str] = set()


def pytest_runtest_logreport(report):
    # A worker can abort while another keeps passing tests. xdist may then exit
    # before pytest renders its error summary, losing the original fixture cause.
    # Forwarded reports have a node only on the controller: print each worker's
    # first fixture failure there immediately, outside worker output capture.
    node = getattr(report, "node", None)
    if (
        node is not None
        and report.when in ("setup", "teardown")
        and report.outcome == "failed"
        and node.gateway.id not in _reported_failure_workers
    ):
        terminal = node.config.pluginmanager.get_plugin("terminalreporter")
        if terminal is not None:
            _reported_failure_workers.add(node.gateway.id)
            terminal.write_sep(
                "!",
                f"First {report.when} failure on {node.gateway.id}: {report.nodeid}",
            )
            terminal.write_line(str(report.longrepr))
    if _doomed_detector.record(report.when, report.outcome):
        pytest.exit(
            f"Aborting: {_doomed_detector.streak} consecutive setup/teardown "
            f"errors with no test passing or failing — the run is doomed by a "
            f"systemic setup failure (e.g. add-on/container setup). Failing fast "
            f"instead of grinding through the suite.",
            returncode=1,
        )
