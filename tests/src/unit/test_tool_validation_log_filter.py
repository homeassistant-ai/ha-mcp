"""Unit tests for ToolValidationLogFilter."""

import logging

import pytest
from fastmcp.exceptions import FastMCPError, ToolError
from pydantic import BaseModel, ValidationError

from ha_mcp.__main__ import ToolValidationLogFilter


def _pydantic_validation_error() -> ValidationError:
    class _Model(BaseModel):
        age: int

    with pytest.raises(ValidationError) as excinfo:
        _Model(age="nope")
    return excinfo.value


class TestToolValidationLogFilter:
    """Verify the filter demotes fastmcp tool-failure tracebacks to WARNING."""

    def setup_method(self):
        self.log_filter = ToolValidationLogFilter()

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

    def test_demotes_validation_error_to_warning(self):
        err = _pydantic_validation_error()
        record = self._make_record(
            "fastmcp.server.server",
            "Error validating tool 'ha_foo'",
            err,
        )
        assert self.log_filter.filter(record) is True
        assert record.levelno == logging.WARNING
        assert record.levelname == "WARNING"
        assert record.exc_info is None
        assert record.exc_text is None
        # Structured error info folded into the message, no pydantic URL.
        assert "age" in record.getMessage()
        assert "errors.pydantic.dev" not in record.getMessage()

    def test_demotes_tool_error_to_warning(self):
        err = ToolError("bad input")
        record = self._make_record(
            "fastmcp.server.server",
            "Error calling tool 'ha_foo'",
            err,
        )
        assert self.log_filter.filter(record) is True
        assert record.levelno == logging.WARNING
        assert record.exc_info is None
        assert "bad input" in record.getMessage()

    def test_passes_bare_exception_through_untouched(self):
        err = RuntimeError("server bug")
        record = self._make_record(
            "fastmcp.server.server",
            "Error calling tool 'ha_foo'",
            err,
        )
        original_exc_info = record.exc_info
        assert self.log_filter.filter(record) is True
        assert record.levelno == logging.ERROR
        assert record.exc_info is original_exc_info

    def test_passes_non_tool_fastmcp_error_through(self):
        # A hypothetical future FastMCPError subclass that is NOT a ToolError
        # (e.g. AuthorizationError) should retain its traceback.
        class FutureAuthError(FastMCPError):
            pass

        err = FutureAuthError("unauthorized")
        record = self._make_record(
            "fastmcp.server.server",
            "Error calling tool 'ha_foo'",
            err,
        )
        assert self.log_filter.filter(record) is True
        assert record.levelno == logging.ERROR
        assert record.exc_info is not None

    def test_leaves_other_loggers_unchanged(self):
        err = ToolError("boom")
        record = self._make_record(
            "some.other.logger",
            "Error calling tool 'ha_foo'",
            err,
        )
        assert self.log_filter.filter(record) is True
        assert record.levelno == logging.ERROR
        assert record.exc_info is not None

    def test_passes_record_without_exc_info(self):
        record = self._make_record(
            "fastmcp.server.server",
            "Error calling tool 'ha_foo'",
            None,
        )
        assert self.log_filter.filter(record) is True
        assert record.levelno == logging.ERROR
