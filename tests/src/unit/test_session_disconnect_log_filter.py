"""Unit tests for SessionDisconnectLogFilter."""

import logging

import anyio
import pytest

from ha_mcp.log_filters import (
    SessionDisconnectLogFilter,
    _is_only_closed_resource_errors,
)


async def _raise_closed_resource_error() -> None:
    raise anyio.ClosedResourceError()


async def _raise_runtime_error() -> None:
    raise RuntimeError("real bug")


async def _run_in_task_group(*coro_funcs) -> BaseException:
    """Run each of ``coro_funcs`` as a task-group child and return the raised
    exception -- reproducing the actual shape mcp.server.lowlevel.server.Server.run()
    produces: it dispatches each incoming message via
    ``anyio.create_task_group().start_soon(self._handle_message, ...)``, so a
    ClosedResourceError raised while responding is raised from a task-group
    child. anyio always wraps that in an ExceptionGroup, even for a single
    failure -- a hand-built ``exc_info`` with a bare exception does not
    reproduce that boundary.
    """
    try:
        async with anyio.create_task_group() as tg:
            for coro_func in coro_funcs:
                tg.start_soon(coro_func)
    except BaseException as exc:
        return exc
    raise AssertionError("task group did not raise")


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

    async def test_demotes_real_task_group_exception_group(self):
        # The shape actually logged in production: mcp.server.lowlevel.server
        # dispatches message handling via anyio.create_task_group().start_soon,
        # so a ClosedResourceError from _send_response arrives here wrapped in
        # an ExceptionGroup, not as a bare exception.
        caught = await _run_in_task_group(_raise_closed_resource_error)
        assert isinstance(caught, BaseExceptionGroup)

        record = self._make_record(
            "mcp.server.streamable_http_manager",
            "Stateless session crashed",
            caught,
        )
        assert self.log_filter.filter(record) is True
        assert record.levelno == logging.WARNING
        assert record.exc_info is None
        assert "client disconnected before response delivery" in record.getMessage()

    async def test_leaves_mixed_exception_group_at_error(self):
        # A task group with one ClosedResourceError AND one unrelated failure
        # signals a real problem alongside the expected disconnect race -- the
        # whole record must stay at ERROR with its traceback intact.
        caught = await _run_in_task_group(
            _raise_closed_resource_error, _raise_runtime_error
        )
        assert isinstance(caught, BaseExceptionGroup)

        record = self._make_record(
            "mcp.server.streamable_http_manager",
            "Stateless session crashed",
            caught,
        )
        original_exc_info = record.exc_info
        assert self.log_filter.filter(record) is True
        assert record.levelno == logging.ERROR
        assert record.exc_info is original_exc_info

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


class TestIsOnlyClosedResourceErrors:
    """Direct coverage of the recursive classifier the filter relies on."""

    def test_bare_closed_resource_error(self):
        assert _is_only_closed_resource_errors(anyio.ClosedResourceError()) is True

    def test_bare_other_exception(self):
        assert _is_only_closed_resource_errors(RuntimeError("x")) is False

    def test_group_of_one_closed_resource_error(self):
        group = ExceptionGroup("eg", [anyio.ClosedResourceError()])
        assert _is_only_closed_resource_errors(group) is True

    def test_nested_group_of_closed_resource_errors(self):
        inner = ExceptionGroup("inner", [anyio.ClosedResourceError()])
        outer = ExceptionGroup("outer", [inner, anyio.ClosedResourceError()])
        assert _is_only_closed_resource_errors(outer) is True

    def test_mixed_group_is_rejected(self):
        group = ExceptionGroup("eg", [anyio.ClosedResourceError(), RuntimeError("x")])
        assert _is_only_closed_resource_errors(group) is False

    def test_empty_group_is_rejected(self):
        # Defensive: an ExceptionGroup always carries at least one exception
        # in practice, but `all([])` is vacuously True -- guard against ever
        # demoting on a group with nothing in it.
        with pytest.raises(ValueError):
            ExceptionGroup("empty", [])


class TestSessionDisconnectLogFilterWiring:
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
