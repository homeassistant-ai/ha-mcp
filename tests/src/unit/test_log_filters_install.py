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


def _make_stale_generation_filter(real_cls: type[logging.Filter]) -> logging.Filter:
    """Build an instance whose (module, qualname) matches ``real_cls`` but whose
    class object is a genuinely DIFFERENT one -- simulating a filter instance
    left over from a previous ``ha_mcp.log_filters`` generation after the
    in-process embedded server purges and re-imports it (see
    ``_purge_ha_mcp_modules``), without actually reloading any module.
    ``importlib.reload()`` mutates the real, shared module in place, which
    would corrupt every OTHER test file's already-imported reference to these
    same class names for the rest of the pytest session -- this builds an
    equivalent stand-in instead.
    """
    stale_cls = type(
        real_cls.__qualname__, (logging.Filter,), {"__module__": real_cls.__module__}
    )
    return stale_cls()


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


class TestInstallSdkLogFiltersReplacesStaleGeneration:
    """The in-process embedded server purges ``ha_mcp.*`` from ``sys.modules``
    and re-imports on every reinstall (see ``_purge_ha_mcp_modules``), so
    ``ha_mcp.log_filters`` is a fresh module with fresh classes after each
    reload -- a stale filter instance from before the reload (a DIFFERENT
    class object with the same name) must be replaced, not left to
    accumulate alongside the new one."""

    def setup_method(self):
        """Snapshot each logger's filters, then plant a stale-generation
        stand-in filter on each -- the state a real reload would leave
        behind, without actually reloading any module."""
        self._saved = {
            name: logging.getLogger(name).filters[:] for name in _TARGET_LOGGERS
        }
        for name, filter_cls in _TARGET_LOGGERS.items():
            logging.getLogger(name).filters[:] = [
                _make_stale_generation_filter(filter_cls)
            ]

    def teardown_method(self):
        """Restore each target logger's filter list, undoing this test's calls."""
        for name, filters in self._saved.items():
            logging.getLogger(name).filters[:] = filters

    def test_stale_generation_instance_is_replaced_not_duplicated(self):
        """A planted stale-generation filter is replaced, never duplicated."""
        stale = {name: logging.getLogger(name).filters[0] for name in _TARGET_LOGGERS}

        install_sdk_log_filters()

        for name, filter_cls in _TARGET_LOGGERS.items():
            filters = logging.getLogger(name).filters
            assert len(filters) == 1, (
                f"{name} must carry exactly one filter after replacing the "
                f"stale generation, got {len(filters)}"
            )
            assert filters[0] is not stale[name], (
                f"{name}'s stale pre-reload filter instance must be replaced, not kept"
            )
            assert isinstance(filters[0], filter_cls), (
                f"{name}'s replacement filter must be the current, real {filter_cls.__name__}"
            )
