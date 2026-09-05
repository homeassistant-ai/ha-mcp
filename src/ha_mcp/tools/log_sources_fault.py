"""``ha_get_logs(source='fault_log')`` — Home Assistant's native-crash dump.

HA Core's ``__main__`` enables :mod:`faulthandler` on ``home-assistant.log.fault``
in the config root. faulthandler writes only on a native fatal signal (SIGSEGV,
SIGABRT, SIGBUS, SIGILL, SIGFPE, or a Python fatal error): the process dies
before any logging runs, so the per-thread Python traceback it dumps reaches
neither journald nor ``home-assistant.log``. No other ``ha_get_logs`` source can
show it (issue #2373).

The file is opened in append mode on every start, so on a healthy install it
exists and is empty; each crash appends one block that opens with a
``Fatal Python error: ...`` line followed by one traceback per thread. A block
is only readable in its original line order, so unlike the line-oriented
raw-text sources this one orders whole blocks and pages through the result
with ``offset`` instead of reversing lines.

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
from .tools_filesystem import _assert_mcp_tools_available, call_mcp_tools_service
from .util_helpers import unwrap_service_response

# Config-relative path HA Core hands to ``faulthandler.enable`` (``FAULT_LOG_FILENAME``
# in ``homeassistant/__main__.py``).
FAULT_LOG_PATH = "home-assistant.log.fault"

# First component release whose read allowlist includes ``FAULT_LOG_PATH``. An
# older component answers the read with "Path not allowed"; that reply is the
# version signal, no separate probe needed.
MIN_COMPONENT_VERSION_FAULT_LOG = "2.1.4"

# Lines tailed from the file per call, independent of ``limit``. A single dump
# lists every thread, so it routinely runs past a 100-line ``limit``; tailing
# only ``limit`` lines would cut off the ``Fatal Python error`` header that
# names the signal. The window is the same one the other raw-text sources use
# for a search, and ``offset`` pages through it.
FAULT_LOG_WINDOW_LINES = SUPERVISOR_SEARCH_WINDOW_LINES

# faulthandler opens every dump with this line, so it delimits crash blocks.
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
        has_more=False,
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
            # Parenthesised so each reads as one suggestion rather than a
            # list entry with a missing comma (py/implicit-string-
            # concatenation-in-list).
            suggestions=[
                (
                    f"Check that {FAULT_LOG_PATH} in the Home Assistant config "
                    "directory is readable by Home Assistant"
                ),
                "Check Home Assistant logs for the ha_mcp_tools read error",
                (
                    "Retry after the file is readable; an absent or empty file "
                    "is reported as no crash, not as an error"
                ),
            ],
            context={"source": "fault_log"},
        )
    )


def _split_blocks(lines: list[str]) -> list[list[str]]:
    """Group the window into crash blocks, each opening with the fatal marker.

    Lines before the first marker are the tail of a block whose header fell
    outside the window; they form a leading partial block so nothing is lost.
    """
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.startswith(_FATAL_MARKER) and current:
            blocks.append(current)
            current = []
        current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _shape_crash_page(
    data: dict[str, Any],
    lines: list[str],
    *,
    limit: int,
    offset: int,
    search: str | None,
    order: Literal["newest", "oldest"],
) -> dict[str, Any]:
    """Order whole crash blocks, then page through the flattened text.

    ``order='newest'`` puts the latest block first; every block keeps its own
    line order so the traceback stays readable. ``search`` selects whole
    blocks (any line matching) rather than lines, so a hit inside a traceback
    still comes back with its ``Fatal Python error`` header and the rest of
    the frames. ``offset``/``limit`` slice the assembled text from its start,
    and ``has_more``/``next_offset`` continue it.
    """
    blocks = _split_blocks(lines)
    fatal_blocks = sum(1 for block in blocks if block[0].startswith(_FATAL_MARKER))
    if order == "newest":
        blocks.reverse()

    filters_applied: dict[str, str] = {}
    if search:
        search_lower = search.lower()
        blocks = [b for b in blocks if any(search_lower in ln.lower() for ln in b)]
        filters_applied["search"] = search
    matched_blocks = len(blocks)
    flat = [line for block in blocks for line in block]

    page = flat[offset : offset + limit]
    has_more = offset + limit < len(flat)
    data.update(
        crash_recorded=True,
        log="\n".join(page),
        returned_lines=len(page),
        fatal_error_blocks_in_window=fatal_blocks,
        has_more=has_more,
    )
    if has_more:
        data["next_offset"] = offset + limit
        # Carry the active filter: the next page must slice the same
        # filtered block list, or offset lands in unrelated blocks.
        search_arg = f", search={search!r}" if search else ""
        data["pagination_hint"] = (
            f"ha_get_logs(source='fault_log', offset={offset + limit}, "
            f"limit={limit}, order='{order}'{search_arg})"
        )
    if filters_applied:
        data["filters_applied"] = filters_applied
        data["matched_blocks"] = matched_blocks
    return data


class FaultLogSourceMixin:
    """``fault_log`` source, mixed into ``LogTools``."""

    _client: Any

    async def _read_fault_file(self) -> dict[str, Any]:
        """One gated ``read_file`` call, unwrapped; transport errors become ToolErrors.

        The live tools-entry probe runs first, as the file and YAML tools do,
        so an entry removed after this client cached its caller token surfaces
        the actionable "add the entry" error rather than HA's 400 for a
        service that no longer exists. That gate and the caller-token gate
        raise their own ToolErrors, which pass through untouched.
        """
        try:
            await _assert_mcp_tools_available(self._client)
            raw = await call_mcp_tools_service(
                self._client,
                "read_file",
                {"path": FAULT_LOG_PATH, "tail_lines": FAULT_LOG_WINDOW_LINES},
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
        offset: int = 0,
        order: Literal["newest", "oldest"] = "newest",
    ) -> dict[str, Any]:
        """Read ``home-assistant.log.fault`` through the component's read_file.

        Always tails :data:`FAULT_LOG_WINDOW_LINES` from the file; ``order``
        arranges whole crash blocks, ``search`` keeps the blocks that mention
        the term, and ``offset``/``limit`` page through the assembled text. An absent or
        empty file is the healthy state and returns success with
        ``crash_recorded=False`` rather than an error.
        """
        effective_limit = _coerce_limit(
            limit, default=DEFAULT_LOG_LIMIT, suggestion_example="100"
        )
        result = await self._read_fault_file()

        data: dict[str, Any] = {
            "success": True,
            "source": "fault_log",
            "path": FAULT_LOG_PATH,
            "limit": effective_limit,
            "offset": offset,
            "order": order,
            "window_lines": FAULT_LOG_WINDOW_LINES,
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
        if isinstance(result.get("modified"), str):
            data["modified"] = result["modified"]

        if not any(line.strip() for line in lines):
            return _no_crash(data, total_lines=total_lines)
        data["total_lines"] = total_lines
        # Older blocks beyond the window are unreachable by offset; say so.
        data["window_truncated"] = bool(result.get("truncated", False))
        return _shape_crash_page(
            data,
            lines,
            limit=effective_limit,
            offset=offset,
            search=search,
            order=order,
        )
