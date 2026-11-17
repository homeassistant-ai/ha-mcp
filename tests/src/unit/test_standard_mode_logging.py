"""Regression tests for standard-mode console logging.

``ha_mcp.utils.usage_logger`` attaches its ``StartupLogCollector`` to the root
logger at import time, so every entry point reaches ``_setup_logging`` with a
root handler already installed — and ``logging.basicConfig`` is a no-op (level
*and* handler) once the root logger has one. The standard-mode entry points
(``ha-mcp-web`` via ``setup_standard_mode``, stdio via ``main``) were therefore
leaving the root logger at WARNING with no console handler, dropping every INFO
line — including the annotated ``GET -> 405 (NORMAL for most non-SSE
connections)`` line that ``browser_landing`` emits in place of the raw uvicorn
access line ``ProbeAccessLogFilter`` drops.

Forcing the reconfiguration is only safe because the collector is kept out of
the sweep: ``basicConfig(force=True)`` removes *and closes* every existing root
handler, which would otherwise silently take ``ha_report_issue``'s startup
diagnostics with it.
"""

from __future__ import annotations

import io
import logging
import sys
from contextlib import contextmanager

import pytest

from ha_mcp import __main__ as ha_main
from ha_mcp.utils import usage_logger

# Exactly what browser_landing._browser_landing logs for a by-design probe 405.
_LANDING_LOG_ARGS = ("GET -> 405 (NORMAL for most non-SSE connections)",)
_LANDING_LOG_TEXT = _LANDING_LOG_ARGS[0]


@contextmanager
def _standard_mode_logging_state():
    """Reproduce the post-import logging state and isolate it from the test run.

    What an entry point really sees when it calls ``_setup_logging``: the root
    logger at WARNING with the ``StartupLogCollector`` as its only handler.

    Applied inside the test body rather than as a fixture because pytest's
    logging plugin attaches its own root handlers for the call phase, i.e.
    after fixture setup. They are swapped out here so ``basicConfig(force=True)``
    cannot close them — a swept handler is closed, not just detached.

    The collector is a fresh instance standing in for the module global so the
    assertions do not depend on the real one's 60-second window still being
    open, and ``sys.stderr`` is a buffer because ``basicConfig`` binds the
    stream at call time.

    Yields ``(collector, stderr_buffer)``.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    # _setup_logging appends a filter to these on every call; restore them or
    # the duplicates leak into the rest of the session.
    saved_filters = [
        (logger, logger.filters[:])
        for logger in (
            logging.getLogger("mcp.server.streamable_http"),
            logging.getLogger("mcp.server.streamable_http_manager"),
            logging.getLogger("fastmcp.server.server"),
        )
    ]
    # These must inherit the root level; reset them to NOTSET so an explicit
    # level left behind by another test cannot mask the assertions.
    ha_loggers = [
        logging.getLogger("ha_mcp"),
        logging.getLogger("ha_mcp.__main__"),
        logging.getLogger("ha_mcp.browser_landing"),
    ]
    saved_levels = [(logger, logger.level) for logger in ha_loggers]
    saved_collector = usage_logger.startup_collector
    saved_stderr = sys.stderr

    collector = usage_logger.StartupLogCollector()
    collector.setLevel(logging.DEBUG)
    stderr_buffer = io.StringIO()
    try:
        for logger in ha_loggers:
            logger.setLevel(logging.NOTSET)
        usage_logger.startup_collector = collector
        root.handlers[:] = [collector]
        root.setLevel(logging.WARNING)
        sys.stderr = stderr_buffer
        yield collector, stderr_buffer
    finally:
        sys.stderr = saved_stderr
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        usage_logger.startup_collector = saved_collector
        for logger, filters in saved_filters:
            logger.filters[:] = filters
        for logger, level in saved_levels:
            logger.setLevel(level)


def _console_handlers() -> list[logging.StreamHandler]:
    return [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, logging.StreamHandler)
    ]


def _start_standard_mode(monkeypatch: pytest.MonkeyPatch, level: str = "INFO") -> None:
    """What ``ha-mcp-web`` runs before it serves, with ``LOG_LEVEL=level``."""
    monkeypatch.setenv("LOG_LEVEL", level)
    monkeypatch.setenv("HOMEASSISTANT_URL", "http://ha.test:8123")
    monkeypatch.setenv("HOMEASSISTANT_TOKEN", "test-token")
    ha_main.setup_standard_mode()


class TestStandardModeConsoleLogging:
    """The logging the standard-mode entry point sets up."""

    def test_installs_stderr_console_handler_at_configured_level(self, monkeypatch):
        with _standard_mode_logging_state() as (_, stderr_buffer):
            _start_standard_mode(monkeypatch)

            console = _console_handlers()
            assert len(console) == 1, (
                "standard mode must install a console handler on the root "
                "logger; the startup collector alone makes basicConfig a no-op"
            )
            # stdio's stdout carries the MCP protocol — console logs go to stderr.
            assert console[0].stream is stderr_buffer
            assert logging.getLogger().level == logging.INFO

    def test_annotated_probe_405_line_reaches_stderr(self, monkeypatch):
        with _standard_mode_logging_state() as (_, stderr_buffer):
            _start_standard_mode(monkeypatch)
            logging.getLogger("ha_mcp.browser_landing").info(*_LANDING_LOG_ARGS)

            assert _LANDING_LOG_TEXT in stderr_buffer.getvalue()

    def test_startup_collector_survives_and_keeps_recording(self, monkeypatch):
        with _standard_mode_logging_state() as (collector, _):
            _start_standard_mode(monkeypatch)

            assert collector in logging.getLogger().handlers, (
                "ha_report_issue's startup-log capture must survive logging setup"
            )

            logging.getLogger("ha_mcp.browser_landing").info(*_LANDING_LOG_ARGS)
            assert any(
                _LANDING_LOG_TEXT in entry["message"]
                for entry in usage_logger.get_startup_logs()
            )

    def test_debug_level_also_keeps_startup_collector(self, monkeypatch):
        """At DEBUG, the level the OAuth/OIDC deployments are debugged at, the
        collector must not be lost either."""
        with _standard_mode_logging_state() as (collector, stderr_buffer):
            _start_standard_mode(monkeypatch, "DEBUG")

            assert logging.getLogger().level == logging.DEBUG
            assert len(_console_handlers()) == 1
            assert collector in logging.getLogger().handlers

            logging.getLogger("ha_mcp.browser_landing").info(*_LANDING_LOG_ARGS)
            assert _LANDING_LOG_TEXT in stderr_buffer.getvalue()
            assert any(
                _LANDING_LOG_TEXT in entry["message"]
                for entry in usage_logger.get_startup_logs()
            )

    def test_force_reconfigures_past_a_foreign_root_handler(self, monkeypatch):
        """Pins the ``force=True`` default itself (Patch76's #2039 review).

        The wrapper detaches only the COLLECTOR, so a foreign root handler —
        pytest's, a library's, or this function's own console handler on a
        repeat call — is what the default actually guards: with one present,
        ``force=False`` makes ``basicConfig`` a silent no-op again (no console
        handler, root stays WARNING). The collector-only cases above stay
        green on a default flip; this one goes red.
        """
        with _standard_mode_logging_state() as (collector, stderr_buffer):
            foreign = logging.NullHandler()
            logging.getLogger().addHandler(foreign)

            _start_standard_mode(monkeypatch)

            console = _console_handlers()
            assert len(console) == 1, (
                "with a foreign root handler present, only force=True "
                "installs the console handler"
            )
            assert console[0].stream is stderr_buffer
            assert logging.getLogger().level == logging.INFO
            # The force sweep removed the foreign handler; the collector was
            # shielded by the wrapper and stays live.
            assert foreign not in logging.getLogger().handlers
            assert collector in logging.getLogger().handlers

    def test_setup_standard_mode_logs_startup_version_to_stderr(self, monkeypatch):
        """End-to-end for the ``ha-mcp-web`` lane: a real INFO line comes out."""
        with _standard_mode_logging_state() as (collector, stderr_buffer):
            _start_standard_mode(monkeypatch)

            # _log_startup_version's `logger.info(f"ha-mcp {version}")`,
            # rendered through _setup_logging's format.
            assert "ha_mcp.__main__ INFO: ha-mcp " in stderr_buffer.getvalue()
            assert collector in logging.getLogger().handlers
            assert any(
                entry["message"].startswith("ha-mcp ")
                for entry in usage_logger.get_startup_logs()
            )
