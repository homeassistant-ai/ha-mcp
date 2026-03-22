"""Unit tests for StatelessSessionLogFilter."""

import logging

from ha_mcp.__main__ import StatelessSessionLogFilter


class TestStatelessSessionLogFilter:
    """Verify the filter annotates stateless termination logs."""

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

    def test_annotates_stateless_termination(self):
        record = self._make_record(
            "mcp.server.streamable_http", "Terminating session: None"
        )
        result = self.log_filter.filter(record)
        assert result is True
        assert record.msg == "Terminating session: None (Normal — stateless mode)"

    def test_leaves_real_session_termination_unchanged(self):
        record = self._make_record(
            "mcp.server.streamable_http", "Terminating session: abc123"
        )
        result = self.log_filter.filter(record)
        assert result is True
        assert record.msg == "Terminating session: abc123"

    def test_leaves_other_loggers_unchanged(self):
        record = self._make_record(
            "some.other.logger", "Terminating session: None"
        )
        result = self.log_filter.filter(record)
        assert result is True
        assert record.msg == "Terminating session: None"

    def test_leaves_unrelated_messages_unchanged(self):
        record = self._make_record(
            "mcp.server.streamable_http", "Processing request"
        )
        result = self.log_filter.filter(record)
        assert result is True
        assert record.msg == "Processing request"
