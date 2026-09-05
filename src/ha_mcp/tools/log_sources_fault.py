"""``ha_get_logs(source='fault_log')`` — Home Assistant's native-crash dump.

HA Core's ``__main__`` enables :mod:`faulthandler` on ``home-assistant.log.fault``
in the config root. faulthandler writes only on a native fatal signal (SIGSEGV,
SIGABRT, SIGBUS, SIGILL, SIGFPE, or a Python fatal error): the process dies
before any logging runs, so the per-thread Python traceback it dumps reaches
neither journald nor ``home-assistant.log``. No other ``ha_get_logs`` source can
show it (issue #2373).

The file is opened in append mode on every start, so on a healthy install it
exists and is empty; each crash appends one ``Fatal Python error: ...`` block.

The read goes through the File & YAML Tools entry's privileged ``read_file``
service (the same route as ``ha_read_file``), which allows the path from
component 2.1.4. Split out of ``log_sources`` under `.gemini/styleguide.md`
§ Tool Consolidation and Module Size.
"""

from typing import Any, Literal, NoReturn

from fastmcp.exceptions import ToolError

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, raise_tool_error
from .log_common import (
    DEFAULT_LOG_LIMIT,
    SUPERVISOR_SEARCH_WINDOW_LINES,
    _coerce_limit,
)
from .tools_filesystem import call_mcp_tools_service
from .util_helpers import unwrap_service_response

# Config-relative path HA Core hands to ``faulthandler.enable`` (``FAULT_LOG_FILENAME``
# in ``homeassistant/__main__.py``).
FAULT_LOG_PATH = "home-assistant.log.fault"

# First component release whose read allowlist includes ``FAULT_LOG_PATH``. An
# older component answers the read with "Path not allowed"; that reply is the
# version signal, no separate probe needed.
MIN_COMPONENT_VERSION_FAULT_LOG = "2.1.4"

# faulthandler prefixes every dump with this line, so counting it counts crashes.
_FATAL_MARKER = "Fatal Python error:"

_NO_CRASH_MESSAGE = (
    "No native crash recorded: home-assistant.log.fault is empty. Ordinary "
    "Python errors never land here; use source='system' or source='error_log' "
    "for those."
)


def _is_missing_file_error(error: str) -> bool:
    return "does not exist" in error or "not a file" in error


def _no_crash(data: dict[str, Any], total_lines: int) -> dict[str, Any]:
    data.update(
        crash_recorded=False,
        log="",
        total_lines=total_lines,
        returned_lines=0,
        message=_NO_CRASH_MESSAGE,
    )
    return data


def _raise_read_failure(error: str) -> NoReturn:
    """Turn a ``read_file`` refusal into the matching structured error.

    "Path not allowed" is the one refusal with a known cause: a component
    older than :data:`MIN_COMPONENT_VERSION_FAULT_LOG`, whose allowlist does
    not yet carry the file. Anything else is reported verbatim.
    """
    if "Path not allowed" in error:
        raise_tool_error(
            create_error_response(
                ErrorCode.COMPONENT_NOT_INSTALLED,
                "The installed ha_mcp_tools custom component does not allow "
                f"reading {FAULT_LOG_PATH} (requires component >= "
                f"{MIN_COMPONENT_VERSION_FAULT_LOG}).",
                details=error,
                suggestions=[
                    "HACS → Integrations → HA-MCP Custom Component → Update",
                    "Restart Home Assistant after the update completes",
                ],
                context={"source": "fault_log"},
            )
        )
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            f"read_file failed for {FAULT_LOG_PATH}: {error or 'unknown error'}",
            context={"source": "fault_log"},
        )
    )


def _shape_crash_window(
    data: dict[str, Any],
    lines: list[str],
    *,
    limit: int,
    search: str | None,
    order: Literal["newest", "oldest"],
) -> dict[str, Any]:
    """Filter, slice and orient the fetched lines like the other raw-text sources."""
    # Count dumps on the fetched window before any search narrows it. The file
    # is append-only across crashes, so a long history can exceed the tail;
    # the count is scoped to the window, not the file.
    fatal_blocks = sum(1 for ln in lines if ln.startswith(_FATAL_MARKER))

    filters_applied: dict[str, str] = {}
    if search:
        search_lower = search.lower()
        lines = [ln for ln in lines if search_lower in ln.lower()]
        filters_applied["search"] = search
    matched_lines = len(lines)

    # Most-recent window; 'order' only flips the display direction.
    lines = lines[-limit:]
    if order == "newest":
        lines = list(reversed(lines))

    data.update(
        crash_recorded=True,
        log="\n".join(lines),
        returned_lines=len(lines),
        fatal_error_blocks_in_window=fatal_blocks,
    )
    if filters_applied:
        data["filters_applied"] = filters_applied
        data["matched_lines"] = matched_lines
    return data


class FaultLogSourceMixin:
    """``fault_log`` source, mixed into ``LogTools``."""

    _client: Any

    async def _read_fault_file(self, tail_lines: int) -> dict[str, Any]:
        """One ``read_file`` call, unwrapped; transport errors become ToolErrors.

        The caller-token gate inside ``call_mcp_tools_service`` raises its own
        actionable ToolError when the File & YAML Tools entry is missing or the
        component predates the bootstrap service; that passes through untouched.
        """
        try:
            raw = await call_mcp_tools_service(
                self._client,
                "read_file",
                {"path": FAULT_LOG_PATH, "tail_lines": tail_lines},
            )
        except ToolError:
            raise
        except (HomeAssistantAuthError, HomeAssistantAPIError) as e:
            exception_to_structured_error(e, context={"source": "fault_log"})
        except (HomeAssistantConnectionError, TimeoutError, OSError) as e:
            exception_to_structured_error(
                e,
                context={"source": "fault_log"},
                suggestions=["Check Home Assistant connection"],
            )
        return unwrap_service_response(raw) if isinstance(raw, dict) else {}

    async def _get_fault_log(
        self,
        limit: int | None = None,
        search: str | None = None,
        order: Literal["newest", "oldest"] = "newest",
    ) -> dict[str, Any]:
        """Tail ``home-assistant.log.fault`` through the component's read_file.

        Mirrors the raw-text sources: ``search`` widens the fetched window,
        ``limit`` slices the most-recent lines of it, ``order`` only sets the
        display direction. An absent or empty file is the healthy state and
        returns success with ``crash_recorded=False`` rather than an error.
        """
        effective_limit = _coerce_limit(
            limit, default=DEFAULT_LOG_LIMIT, suggestion_example="100"
        )
        fetch_lines = (
            max(effective_limit, SUPERVISOR_SEARCH_WINDOW_LINES)
            if search
            else effective_limit
        )
        result = await self._read_fault_file(fetch_lines)

        data: dict[str, Any] = {
            "success": True,
            "source": "fault_log",
            "path": FAULT_LOG_PATH,
            "limit": effective_limit,
            "order": order,
        }
        if not result.get("success", False):
            error = str(result.get("error", ""))
            if _is_missing_file_error(error):
                # HA opens the file at every start, so a missing file means
                # this config dir has not been started by HA's __main__ (or
                # someone removed it). Either way: nothing to show.
                return _no_crash(data, total_lines=0)
            _raise_read_failure(error)

        content = result.get("content")
        lines = content.splitlines() if isinstance(content, str) else []
        # The component reports the untailed line count; fall back to what we
        # got when an older shape omits it.
        total_lines = result.get("total_lines")
        if not isinstance(total_lines, int):
            total_lines = len(lines)
        data["window_lines"] = fetch_lines
        if isinstance(result.get("modified"), str):
            data["modified"] = result["modified"]

        if not any(line.strip() for line in lines):
            return _no_crash(data, total_lines=total_lines)
        data["total_lines"] = total_lines
        return _shape_crash_window(
            data, lines, limit=effective_limit, search=search, order=order
        )
