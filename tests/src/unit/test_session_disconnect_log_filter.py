"""Unit tests for SessionDisconnectLogFilter."""

import logging

import anyio

from ha_mcp.__main__ import SessionDisconnectLogFilter


class TestSessionDisconnectLogFilter:
    """Verify the filter demotes disconnect-caused 'session crashed' tracebacks."""

    def setup_method(self):
        self.log_filter = SessionDisconnectLogFilter()

    def _make_record(
        self,
        name: str,
        msg: str,
        exc: BaseException | None,
    ) -> logging.LogRecord:
        exc_info = (type(exc), exc, None) if exc is not None else None
        return logging.LogRecord(
            name=name,
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg=msg,
            args=(),
            exc_info=exc_info,
        )

    def test_demotes_stateless_session_crash_from_closed_resource_error(self):
        # Exactly what mcp/server/streamable_http_manager.py's
        # _handle_stateless_request logs when the client already disconnected.
        err = anyio.ClosedResourceError()
        record = self._make_record(
            "mcp.server.streamable_http_manager",
            "Stateless session crashed",
            err,
        )
        assert self.log_filter.filter(record) is True
        assert record.levelno == logging.WARNING
        assert record.levelname == "WARNING"
        assert record.exc_info is None
        assert record.exc_text is None
        assert "client disconnected before response delivery" in record.getMessage()
        assert "Stateless session crashed" in record.getMessage()

    def test_demotes_stateful_session_crash_from_closed_resource_error(self):
        # The stateful runner's equivalent log line -- same race, same fix,
        # even though every HTTP entry point currently forces stateless_http.
        err = anyio.ClosedResourceError()
        record = self._make_record(
            "mcp.server.streamable_http_manager",
            "Session abc123 crashed",
            err,
        )
        assert self.log_filter.filter(record) is True
        assert record.levelno == logging.WARNING
        assert record.exc_info is None

    def test_passes_bare_exception_through_untouched(self):
        # An actual server bug on this logger must keep its traceback and
        # ERROR level -- only the known-benign disconnect race is demoted.
        err = RuntimeError("server bug")
        record = self._make_record(
            "mcp.server.streamable_http_manager",
            "Stateless session crashed",
            err,
        )
        original_exc_info = record.exc_info
        assert self.log_filter.filter(record) is True
        assert record.levelno == logging.ERROR
        assert record.exc_info is original_exc_info

    def test_leaves_other_loggers_unchanged(self):
        err = anyio.ClosedResourceError()
        record = self._make_record(
            "some.other.logger",
            "Stateless session crashed",
            err,
        )
        assert self.log_filter.filter(record) is True
        assert record.levelno == logging.ERROR
        assert record.exc_info is not None

    def test_passes_record_without_exc_info(self):
        record = self._make_record(
            "mcp.server.streamable_http_manager",
            "Stateless session crashed",
            None,
        )
        assert self.log_filter.filter(record) is True
        assert record.levelno == logging.ERROR

    def test_setup_logging_wires_filter_and_demotes_output(self, monkeypatch):
        """Integration: ``_setup_logging`` attaches the filter to the SDK's
        session-manager logger, so a real ``ClosedResourceError``-caused
        'Stateless session crashed' entry loses its traceback and ERROR level
        in actual output, while an unrelated exception on the same logger
        still comes through as a full ERROR traceback.

        ``logging.basicConfig`` is stubbed to a no-op for the same reason as
        the sibling ``StatelessSessionLogFilter`` wiring test: its real
        ``basicConfig(force=True)`` would tear down and *close* the root
        logger's handlers, including pytest's capture handlers.
        """
        import io

        from ha_mcp import __main__ as ha_main

        sdk_logger = logging.getLogger("mcp.server.streamable_http_manager")
        # _setup_logging also attaches filters to sibling loggers; save/restore
        # them too or they leak into other tests.
        stateless_logger = logging.getLogger("mcp.server.streamable_http")
        fastmcp_logger = logging.getLogger("fastmcp.server.server")
        saved_sdk_filters = sdk_logger.filters[:]
        saved_stateless_filters = stateless_logger.filters[:]
        saved_fastmcp_filters = fastmcp_logger.filters[:]
        saved_propagate = sdk_logger.propagate
        saved_level = sdk_logger.level
        monkeypatch.setattr(ha_main.logging, "basicConfig", lambda *a, **k: None)
        try:
            ha_main._setup_logging("INFO", force=True)
            assert any(
                isinstance(f, SessionDisconnectLogFilter) for f in sdk_logger.filters
            ), "_setup_logging must attach SessionDisconnectLogFilter to the SDK's session-manager logger"

            disconnect_buf = io.StringIO()
            handler = logging.StreamHandler(disconnect_buf)
            handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
            sdk_logger.addHandler(handler)
            sdk_logger.setLevel(logging.INFO)
            sdk_logger.propagate = False
            try:
                try:
                    raise anyio.ClosedResourceError()
                except anyio.ClosedResourceError:
                    sdk_logger.exception("Stateless session crashed")
            finally:
                sdk_logger.removeHandler(handler)

            disconnect_out = disconnect_buf.getvalue()
            assert "client disconnected before response delivery" in disconnect_out
            assert "WARNING" in disconnect_out
            assert "Traceback" not in disconnect_out, (
                "the ClosedResourceError-caused crash must lose its traceback"
            )

            bug_buf = io.StringIO()
            handler = logging.StreamHandler(bug_buf)
            handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
            sdk_logger.addHandler(handler)
            try:
                try:
                    raise RuntimeError("real bug")
                except RuntimeError:
                    sdk_logger.exception("Stateless session crashed")
            finally:
                sdk_logger.removeHandler(handler)

            bug_out = bug_buf.getvalue()
            assert "real bug" in bug_out
            assert "Traceback" in bug_out, (
                "an unrelated exception on this logger must keep its traceback"
            )
        finally:
            sdk_logger.filters[:] = saved_sdk_filters
            stateless_logger.filters[:] = saved_stateless_filters
            fastmcp_logger.filters[:] = saved_fastmcp_filters
            sdk_logger.propagate = saved_propagate
            sdk_logger.setLevel(saved_level)
