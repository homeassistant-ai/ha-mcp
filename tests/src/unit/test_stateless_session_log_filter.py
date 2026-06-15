"""Unit tests for StatelessSessionLogFilter."""

import logging

from ha_mcp.__main__ import StatelessSessionLogFilter


class TestStatelessSessionLogFilter:
    """Verify the filter suppresses routine stateless termination logs."""

    def setup_method(self):
        self.log_filter = StatelessSessionLogFilter()

    def _make_record(self, name: str, msg: str) -> logging.LogRecord:
        return logging.LogRecord(
            name=name,
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=msg,
            args=(),
            exc_info=None,
        )

    def test_suppresses_stateless_termination(self):
        record = self._make_record(
            "mcp.server.streamable_http", "Terminating session: None"
        )
        # Dropped entirely (not just relabelled): filter returns False.
        assert self.log_filter.filter(record) is False

    def test_suppresses_printf_style_termination(self):
        record = self._make_record(
            "mcp.server.streamable_http", "Terminating session: %s"
        )
        record.args = (None,)
        assert self.log_filter.filter(record) is False

    def test_keeps_real_session_termination(self):
        record = self._make_record(
            "mcp.server.streamable_http", "Terminating session: abc123"
        )
        # A real session id is meaningful — keep it, unchanged.
        assert self.log_filter.filter(record) is True
        assert record.levelno == logging.INFO

    def test_keeps_other_loggers(self):
        record = self._make_record("some.other.logger", "Terminating session: None")
        # Only suppress on the SDK's streamable_http logger.
        assert self.log_filter.filter(record) is True
        assert record.levelno == logging.INFO

    def test_keeps_unrelated_messages(self):
        record = self._make_record("mcp.server.streamable_http", "Processing request")
        assert self.log_filter.filter(record) is True
        assert record.levelno == logging.INFO

    def test_keeps_record_with_unrenderable_format(self):
        """A record whose %-format can't be rendered (more specifiers than args)
        must not raise out of the filter -- filters run in Logger.handle() with
        no exception guard -- and is kept (fail-open)."""
        record = logging.LogRecord(
            name="mcp.server.streamable_http",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="malformed %s %s",
            args=("only-one",),
            exc_info=None,
        )
        assert self.log_filter.filter(record) is True

    def test_setup_logging_wires_filter_and_suppresses_output(self, monkeypatch):
        """Integration: ``_setup_logging`` attaches the filter to the SDK logger,
        so the real ``Terminating session: None`` INFO call (the stateless
        per-request teardown) produces NO log output, while a real-id
        termination still does.

        This is the mechanism the HAOS add-on and the Docker HTTP lane both rely
        on -- both start via ``ha-mcp-web`` -> ``_setup_logging`` -> this
        ``addFilter`` call -- so it guards the actual user-visible behaviour, not
        just the filter in isolation.

        ``logging.basicConfig`` is stubbed to a no-op: only ``_setup_logging``'s
        ``addFilter`` wiring matters here, and its real ``basicConfig(force=True)``
        would tear down and *close* the root logger's handlers -- including
        pytest's capture handlers -- polluting other tests in the same worker.
        """
        import io

        from ha_mcp import __main__ as ha_main

        sdk_logger = logging.getLogger("mcp.server.streamable_http")
        # _setup_logging also attaches ToolValidationLogFilter to the
        # fastmcp.server.server logger; save/restore it too or it leaks.
        fastmcp_logger = logging.getLogger("fastmcp.server.server")
        saved_sdk_filters = sdk_logger.filters[:]
        saved_fastmcp_filters = fastmcp_logger.filters[:]
        saved_propagate = sdk_logger.propagate
        saved_level = sdk_logger.level
        # Keep _setup_logging from reconfiguring (and closing the handlers of)
        # the root logger; we only exercise its addFilter() wiring here.
        monkeypatch.setattr(ha_main.logging, "basicConfig", lambda *a, **k: None)
        try:
            ha_main._setup_logging("INFO", force=True)
            assert any(
                isinstance(f, StatelessSessionLogFilter) for f in sdk_logger.filters
            ), "_setup_logging must attach StatelessSessionLogFilter to the SDK logger"

            buf = io.StringIO()
            handler = logging.StreamHandler(buf)
            sdk_logger.addHandler(handler)
            sdk_logger.setLevel(logging.INFO)  # basicConfig stubbed; enable INFO here
            sdk_logger.propagate = False  # isolate capture to our handler
            try:
                # Exactly what mcp/server/streamable_http.py emits per request:
                sdk_logger.info("Terminating session: None")
                sdk_logger.info("Terminating session: 7c3f-real-session")
            finally:
                sdk_logger.removeHandler(handler)

            out = buf.getvalue()
            assert "Terminating session: None" not in out  # suppressed
            assert "7c3f-real-session" in out  # real terminations still logged
        finally:
            sdk_logger.filters[:] = saved_sdk_filters
            fastmcp_logger.filters[:] = saved_fastmcp_filters
            sdk_logger.propagate = saved_propagate
            sdk_logger.setLevel(saved_level)
