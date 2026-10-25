"""Unit tests for install_sdk_log_filters()'s idempotency."""

import logging

from ha_mcp.log_filters import (
    SessionDisconnectLogFilter,
    StatelessSessionLogFilter,
    install_sdk_log_filters,
    route_fastmcp_logs_to_root,
)

_TARGET_LOGGERS = {
    "mcp.server.streamable_http": StatelessSessionLogFilter,
    "mcp.server.streamable_http_manager": SessionDisconnectLogFilter,
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


class TestRouteFastmcpLogsToRoot:
    """The embedded server hands FastMCP's records to HA's logging."""

    def test_clears_handlers_and_enables_propagation(self):
        logger = logging.getLogger("fastmcp")
        saved_handlers = logger.handlers[:]
        saved_propagate = logger.propagate
        try:
            logger.handlers[:] = []
            logger.addHandler(logging.StreamHandler())
            logger.propagate = False

            route_fastmcp_logs_to_root()

            assert logger.handlers == []
            assert logger.propagate is True
        finally:
            logger.handlers[:] = saved_handlers
            logger.propagate = saved_propagate

    def test_install_routes_logs_only_when_embedded(self, monkeypatch):
        from ha_mcp import config, log_filters

        calls: list[bool] = []
        monkeypatch.setattr(
            log_filters, "route_fastmcp_logs_to_root", lambda: calls.append(True)
        )

        monkeypatch.setattr(config, "is_embedded_mode", lambda: True)
        log_filters.install_sdk_log_filters()
        assert calls == [True]

        calls.clear()
        monkeypatch.setattr(config, "is_embedded_mode", lambda: False)
        log_filters.install_sdk_log_filters()
        assert calls == []

    def test_the_forwarding_filter_keeps_home_assistants_threshold(self):
        import logging as lg

        from ha_mcp.log_filters import _RootThresholdFilter

        root = lg.getLogger()
        saved = root.level
        try:
            root.setLevel(lg.WARNING)
            threshold = _RootThresholdFilter()
            info = lg.LogRecord("fastmcp", lg.INFO, __file__, 1, "info", None, None)
            warning = lg.LogRecord(
                "fastmcp", lg.WARNING, __file__, 1, "warning", None, None
            )
            assert not threshold.filter(info)
            assert threshold.filter(warning)
        finally:
            root.setLevel(saved)

    def test_the_threshold_filter_covers_child_loggers(self):
        """Logger filters do not cascade, so the handler filter must see them."""
        import logging as lg

        from ha_mcp.log_filters import _RootThresholdFilter

        root = lg.getLogger()
        saved = root.level
        try:
            root.setLevel(lg.WARNING)
            threshold = _RootThresholdFilter()
            child = lg.LogRecord(
                "fastmcp.server.lowlevel", lg.INFO, __file__, 1, "info", None, None
            )
            other = lg.LogRecord(
                "homeassistant.core", lg.INFO, __file__, 1, "info", None, None
            )
            assert not threshold.filter(child)
            # Only FastMCP records are gated; HA's own logging is untouched.
            assert threshold.filter(other)
        finally:
            root.setLevel(saved)

    def test_route_installs_the_threshold_on_root_handlers_once(self):
        import logging as lg

        from ha_mcp.log_filters import _RootThresholdFilter

        root = lg.getLogger()
        saved_handlers = root.handlers[:]
        try:
            root.handlers[:] = [lg.StreamHandler()]
            route_fastmcp_logs_to_root()
            route_fastmcp_logs_to_root()
            filters = root.handlers[0].filters
            assert sum(isinstance(f, _RootThresholdFilter) for f in filters) == 1
        finally:
            root.handlers[:] = saved_handlers
