"""Doomed-run detector for the e2e fail-fast hook.

Extracted from ``tests/src/e2e/conftest.py`` so the streak logic is unit-testable
without importing the heavy e2e conftest. See
``tests/src/e2e/conftest.py::pytest_runtest_logreport`` for the wiring and
``tests/src/unit/test_doomed_run.py`` for the tests.
"""

from __future__ import annotations

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
