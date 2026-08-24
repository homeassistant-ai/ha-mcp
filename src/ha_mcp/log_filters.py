"""MCP SDK / fastmcp log-noise filters shared across every HTTP launcher.

Extracted from :mod:`ha_mcp.__main__` for the same reason as
:mod:`ha_mcp.browser_landing`: the in-process server (the ``ha_mcp_tools``
custom-component worker thread) must never import ``ha_mcp.__main__``, since
that module runs process-global side effects at import time
(``truststore.inject_into_ssl()``, signal handlers, ``asyncio.run``). These
filters only call ``logging.Logger.addFilter`` on specific named loggers --
no ``basicConfig``, no handler or root-logger changes -- so
``install_sdk_log_filters()`` is safe to call from any launcher, including
one that must leave Home Assistant's own logging configuration untouched
(see the ``log_config=None`` comment in ``embedded_server.py``).
"""

from __future__ import annotations

import logging

import anyio
from fastmcp.exceptions import ToolError
from pydantic import ValidationError as PydanticValidationError


class StatelessSessionLogFilter(logging.Filter):
    """Suppress the routine 'Terminating session: None' log from the MCP SDK.

    In stateless HTTP mode every request creates and tears down a temporary
    session whose id is ``None``, so the SDK emits an INFO
    ``Terminating session: None`` (mcp/server/streamable_http.py) on *every*
    request. The line is routine but looks alarming and has repeatedly
    confused users into thinking the connection is broken.

    Returning ``False`` drops the record at this logger before it reaches any
    handler. (Merely downgrading the level to DEBUG did not work: the level
    gate is applied before the filter runs, so the record was already admitted
    and still emitted -- just relabelled.) Real session terminations carry an
    actual id and are not matched, so they still log.

    # TODO: remove when modelcontextprotocol/python-sdk#2329 is resolved
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Drop the routine stateless-teardown record; pass everything else."""
        if record.name != "mcp.server.streamable_http":
            return True
        try:
            message = record.getMessage()
        except (ValueError, TypeError):
            # A malformed %-format record on this logger is not our target, and
            # a filter must not raise: filters run in Logger.handle() with no
            # exception handling, so a raise would crash the logging call.
            return True
        # Drop the stateless teardown noise; keep everything else.
        return "Terminating session: None" not in message


class ToolValidationLogFilter(logging.Filter):
    """Demote fastmcp tool-failure tracebacks to single-line warnings.

    Pydantic ValidationError and tool-raised ToolError aren't server bugs,
    so the traceback through fastmcp/pydantic internals is just noise. The
    structured error detail is preserved in the WARNING message; stack is
    intentionally dropped because these are user-input errors, not bugs.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Demote a known-benign validation/tool-error record to WARNING."""
        if record.name != "fastmcp.server.server" or not record.exc_info:
            return True

        msg = record.getMessage()
        err = record.exc_info[1]
        if "Error validating tool" in msg and isinstance(err, PydanticValidationError):
            record.msg = f"{msg}: {err.errors(include_url=False)}"
        elif "Error calling tool" in msg and isinstance(err, ToolError):
            record.msg = f"{msg}: {err}"
        else:
            return True

        record.args = ()
        record.levelno = logging.WARNING
        record.levelname = "WARNING"
        record.exc_info = None
        record.exc_text = None
        return True


def _is_only_closed_resource_errors(err: BaseException) -> bool:
    """True if ``err`` is (or an ExceptionGroup wrapping only) ClosedResourceError.

    ``mcp.server.lowlevel.server.Server.run()`` dispatches each incoming
    message via ``anyio.create_task_group().start_soon(...)``, so a
    ``ClosedResourceError`` raised while delivering one message's response
    is raised from a task-group child -- anyio always wraps that in an
    ``ExceptionGroup``, even for a single failure. The exception a "session
    crashed" log record carries is therefore
    ``ExceptionGroup(...[ClosedResourceError])``, not a bare
    ``ClosedResourceError``. Recurse through (possibly nested) groups; any
    non-``ClosedResourceError`` leaf means this is not the known-benign
    disconnect race, so the caller should leave the record alone.
    """
    if isinstance(err, anyio.ClosedResourceError):
        return True
    if isinstance(err, BaseExceptionGroup):
        return bool(err.exceptions) and all(
            _is_only_closed_resource_errors(sub) for sub in err.exceptions
        )
    return False


class SessionDisconnectLogFilter(logging.Filter):
    """Demote 'session crashed' tracebacks caused by an already-gone client.

    Every HTTP entry point runs Streamable HTTP in stateless mode (see
    ``ha_mcp.__main__._http_run_kwargs``). A tool call slow enough to outlast
    the client's patience -- a busy Home Assistant instance, a
    resource-contended local LLM host on the client side, or an ordinary HTTP
    timeout -- lets the SDK finish serving the response and tear the
    transport down while ``app.run()`` is still working; the eventual attempt
    to deliver the response then writes into an already-closed memory stream
    and raises ``anyio.ClosedResourceError`` (wrapped in an ``ExceptionGroup``
    -- see ``_is_only_closed_resource_errors``). The SDK already catches this
    (``except Exception: logger.exception(...)`` in both the stateless and
    stateful session runners of mcp/server/streamable_http_manager.py) -- it
    just logs it as an alarming ERROR-level traceback. That's an expected
    race in a stateless HTTP protocol (the client already gave up), not a
    server bug, so demote it the same way ToolValidationLogFilter demotes
    other known-benign failures. Any other exception on this logger -- an
    actual crash -- is left untouched.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Demote a disconnect-caused 'session crashed' record to WARNING."""
        if record.name != "mcp.server.streamable_http_manager" or not record.exc_info:
            return True

        err = record.exc_info[1]
        if err is None or not _is_only_closed_resource_errors(err):
            return True

        record.msg = (
            f"{record.getMessage()}: client disconnected before response delivery"
        )
        record.args = ()
        record.levelno = logging.WARNING
        record.levelname = "WARNING"
        record.exc_info = None
        record.exc_text = None
        return True


def _add_filter_once(logger_name: str, filter_cls: type[logging.Filter]) -> None:
    """Attach one ``filter_cls`` instance to ``logger_name``, unless already present.

    ``install_sdk_log_filters()`` can run more than once per process: the
    in-process embedded server calls it on every ``_serve()`` reload without
    a process restart, and process-wide ``logging`` state (including each
    named logger's filter list) persists across those reloads. Without this
    guard, filters would accumulate one more redundant instance per reload.
    """
    logger = logging.getLogger(logger_name)
    if any(isinstance(f, filter_cls) for f in logger.filters):
        return
    logger.addFilter(filter_cls())


def install_sdk_log_filters() -> None:
    """Attach the demotion filters above to their target SDK/fastmcp loggers.

    Every HTTP launcher must call this: the CLI (``ha_mcp.__main__``), the
    Home Assistant app's ``start.py``, and the in-process embedded server
    (``ha_mcp_tools/embedded_server.py``) each build and run their own
    Streamable HTTP app, so none of them share another launcher's logging
    setup. Safe to call repeatedly -- see ``_add_filter_once``.
    """
    _add_filter_once("mcp.server.streamable_http", StatelessSessionLogFilter)
    _add_filter_once("mcp.server.streamable_http_manager", SessionDisconnectLogFilter)
    _add_filter_once("fastmcp.server.server", ToolValidationLogFilter)
