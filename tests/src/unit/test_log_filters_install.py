"""Unit tests for install_sdk_log_filters()'s idempotency."""

import logging

from ha_mcp.log_filters import (
    SessionDisconnectLogFilter,
    StatelessSessionLogFilter,
    ToolValidationLogFilter,
    install_sdk_log_filters,
)

_TARGET_LOGGERS = {
    "mcp.server.streamable_http": StatelessSessionLogFilter,
    "mcp.server.streamable_http_manager": SessionDisconnectLogFilter,
    "fastmcp.server.server": ToolValidationLogFilter,
}


class TestInstallSdkLogFiltersIdempotent:
    """The in-process embedded server calls this on every reload without a
    process restart, so process-wide logger filter lists persist across
    calls -- a second call must not add a second instance of each filter."""

    def setup_method(self):
        """Snapshot each target logger's current filter list."""
        self._saved = {
            name: logging.getLogger(name).filters[:] for name in _TARGET_LOGGERS
        }

    def teardown_method(self):
        """Restore each target logger's filter list, undoing this test's calls."""
        for name, filters in self._saved.items():
            logging.getLogger(name).filters[:] = filters

    def test_second_call_does_not_duplicate_filters(self):
        """Calling the installer twice must not attach a filter twice."""
        install_sdk_log_filters()
        install_sdk_log_filters()

        for name, filter_cls in _TARGET_LOGGERS.items():
            matches = [
                f for f in logging.getLogger(name).filters if isinstance(f, filter_cls)
            ]
            assert len(matches) == 1, (
                f"{name} must carry exactly one {filter_cls.__name__}, got {len(matches)}"
            )
