"""Regression tests for StartupLogCollector reentrancy (issue #2357).

The collector is attached to the ROOT logger at DEBUG with no filter, and in
embedded mode it runs inside the Home Assistant process — so every debug record
from every integration is formatted on the event loop thread. Formatting is not
inert: ``%s`` of an entity calls ``Entity.__repr__`` -> ``_stringify_state`` ->
the entity's ``state`` property, and an integration whose property logs at debug
re-enters ``emit`` on the same thread.

That re-entry used to land on ``with self._lock`` while the outer frame still
held the same non-reentrant ``threading.Lock`` — a self-deadlock that froze all
of Home Assistant (reported against tapo_control's ``latest_version`` property).
"""

import logging
import threading

from ha_mcp.utils.usage_logger import (
    MAX_STARTUP_LOG_ENTRIES,
    StartupLogCollector,
)

# Generous enough that a slow CI runner never flakes, short enough that the
# pre-fix deadlock (which never completes) is caught quickly.
DEADLOCK_TIMEOUT_SECONDS = 10.0


class _LogsOnRepr:
    """Stand-in for an entity whose ``__repr__`` reaches a logging property.

    Models the real chain without importing Home Assistant: tapo_control's
    ``latest_version`` property calls ``_LOGGER.debug()``, and HA's update
    entity reaches that property from ``__repr__`` via ``state``.
    """

    def __init__(self, log: logging.Logger, depth: int = 1):
        self._log = log
        self._depth = depth

    def __repr__(self) -> str:
        if self._depth > 0:
            self._log.debug("nested %s", _LogsOnRepr(self._log, self._depth - 1))
        return "<entity>"


def _make_logger(name: str, collector: StartupLogCollector) -> logging.Logger:
    log = logging.getLogger(name)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    log.handlers = [collector]
    return log


def _emit_in_thread(target) -> bool:
    """Run ``target`` in a worker thread; return True if it finished in time.

    A deadlocked thread cannot be killed, so the worker is a daemon: a failure
    here leaves it wedged for the rest of the session rather than hanging the
    whole pytest run.
    """
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(DEADLOCK_TIMEOUT_SECONDS)
    return not thread.is_alive()


class TestStartupLogCollectorReentrancy:
    """A log record whose formatting logs again must not wedge the thread."""

    def test_reentrant_formatting_does_not_deadlock(self):
        collector = StartupLogCollector()
        log = _make_logger("test_2357.deadlock", collector)

        finished = _emit_in_thread(lambda: log.debug("outer %s", _LogsOnRepr(log)))

        assert finished, (
            "emit() deadlocked on a re-entrant log record — this is the #2357 "
            "freeze: formatting inside self._lock re-enters emit on the same "
            "thread and blocks on a non-reentrant Lock."
        )

    def test_outer_record_survives_nested_drop(self):
        collector = StartupLogCollector()
        log = _make_logger("test_2357.outer", collector)

        assert _emit_in_thread(lambda: log.debug("outer %s", _LogsOnRepr(log)))

        entries = collector.get_logs()
        # The nested record is dropped by the reentrancy guard; the outer one,
        # which is the record that was actually logged, is still collected —
        # and the drop is accounted for, not hidden, in the diagnostics.
        assert [entry["message"] for entry in entries[:-1]] == ["outer <entity>"]
        assert entries[-1]["level"] == "WARNING"
        assert "1 nested log record(s)" in entries[-1]["message"]

    def test_no_drop_marker_when_nothing_was_dropped(self):
        collector = StartupLogCollector()
        log = _make_logger("test_2357.clean", collector)

        log.debug("plain record")

        assert [entry["message"] for entry in collector.get_logs()] == ["plain record"]

    def test_guard_clears_after_reentrant_record(self):
        collector = StartupLogCollector()
        log = _make_logger("test_2357.reset", collector)

        def outer_then_later() -> None:
            log.debug("outer %s", _LogsOnRepr(log))
            log.debug("later record")

        # Both on the same thread: the guard is per-thread, so a flag left set
        # by the reentrant record would only ever affect that thread.
        assert _emit_in_thread(outer_then_later)

        messages = [entry["message"] for entry in collector.get_logs()]
        # A stuck per-thread flag would silently drop every later record on
        # that thread for the rest of the startup window.
        assert "later record" in messages

    def test_deeply_nested_formatting_terminates(self):
        collector = StartupLogCollector()
        log = _make_logger("test_2357.deep", collector)

        assert _emit_in_thread(
            lambda: log.debug("outer %s", _LogsOnRepr(log, depth=25))
        )

    def test_unformattable_record_is_recorded_not_raised(self):
        class Explodes:
            def __repr__(self) -> str:
                raise ValueError("boom")

        collector = StartupLogCollector()
        log = _make_logger("test_2357.unformattable", collector)

        handled: list[logging.LogRecord] = []
        collector.handleError = handled.append  # type: ignore[method-assign]

        log.debug("bad %s", Explodes())

        entries = collector.get_logs()
        assert len(entries) == 1
        assert "unformattable" in entries[0]["message"]
        assert "ValueError" in entries[0]["message"]
        # Names the call site, not just the logger.
        assert f"{__file__}:" in entries[0]["message"]
        # The stdlib error path still runs, so raiseExceptions/stderr apply.
        assert len(handled) == 1

    def test_collection_stops_at_entry_cap(self):
        collector = StartupLogCollector()
        log = _make_logger("test_2357.cap", collector)

        for index in range(MAX_STARTUP_LOG_ENTRIES + 50):
            log.debug("entry %d", index)

        entries = collector.get_logs()
        assert len(entries) == MAX_STARTUP_LOG_ENTRIES
        # Earliest entries are the ones kept — boot order is what the startup
        # diagnostics are for.
        assert entries[0]["message"] == "entry 0"
        assert collector.is_active() is False
