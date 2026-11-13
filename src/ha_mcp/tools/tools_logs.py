"""``ha_get_logs`` — one tool over every Home Assistant log source.

Holds the source dispatch, the cross-source parameter rules, and the tool
registration. The sources themselves are mixed in from ``log_sources`` (Core)
and ``log_sources_supervisor`` (Supervisor), their shared plumbing lives in
``log_common``, and the error-log window arithmetic in ``error_log_parsing``.

Split out of ``tools_utility`` under `.gemini/styleguide.md` § Tool
Consolidation and Module Size.
"""

from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import Field

from .._vendor.fastmcp.exceptions import ToolError
from ..client.error_log_journald import is_cursor_token, parse_cursor_token
from ..client.journal_window import parse_window_token
from ..errors import TOOL_ERROR_LOG_LEVEL
from .error_log_parsing import _DEFAULT_TOP_N
from .helpers import log_tool_usage
from .log_common import (
    MAX_LIMIT,
    _collect_log_warnings,
    _validate_log_level,
    _validate_log_slug,
)
from .log_sources import _SYSTEM_LOG_MESSAGE_CAP, CoreLogSourcesMixin
from .log_sources_fault import FaultLogSourceMixin
from .log_sources_logbook import LogbookLogSourceMixin
from .log_sources_supervisor import SupervisorLogSourcesMixin
from .tool_hints import read_only_hints


def _validate_offset_token(source: str, token: str) -> str:
    """Check ``token`` is a ``next_offset`` value ``source`` itself returns."""
    if source in ("supervisor", "system_service"):
        parse: Callable[[str], object] = parse_window_token
    elif source == "error_log":
        if not is_cursor_token(token):
            raise ToolError(
                "Invalid 'offset' token for source='error_log'; pass the "
                "'next_offset' value a previous response returned",
                log_level=TOOL_ERROR_LOG_LEVEL,
            )
        parse = parse_cursor_token
    else:
        raise ToolError(
            "Cursor tokens only apply to source='error_log', 'supervisor' "
            f"and 'system_service'; source='{source}' takes a non-negative "
            "integer offset",
            log_level=TOOL_ERROR_LOG_LEVEL,
        )
    try:
        parse(token)
    except ValueError as e:
        raise ToolError(
            f"Invalid 'offset' token for source='{source}' ({e}); pass the "
            "'next_offset' value a previous response returned",
            log_level=TOOL_ERROR_LOG_LEVEL,
        ) from e
    return token


def _validate_log_offset(source: str, offset: int | str) -> int | str:
    """Validate a paging anchor: an integer, or a cursor token a page returned."""
    if isinstance(offset, str):
        text = offset.strip()
        if not text:
            # An empty string means "no anchor"; MCP clients stringify 0.
            return 0
        if not text.lstrip("-").isdigit():
            return _validate_offset_token(source, text)
        # Clients that stringify integers used to reach the integer schema;
        # keep that working now that the union admits strings.
        offset = int(text)
    if offset < 0:
        raise ToolError(
            f"'offset' must not be negative, got {offset}",
            log_level=TOOL_ERROR_LOG_LEVEL,
        )
    return offset


class LogTools(
    LogbookLogSourceMixin,
    CoreLogSourcesMixin,
    SupervisorLogSourcesMixin,
    FaultLogSourceMixin,
):
    """Dispatches ``ha_get_logs`` to one log source and shapes the response."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _fetch_log_source(
        self,
        source: str,
        limit: int | None,
        search: str | None,
        hours_back: int | None,
        entity_id: str | None,
        device_id: str | None,
        context_id: str | None,
        end_time: str | None,
        offset: int | str,
        compact: bool,
        level: str | None,
        slug: str | None,
        order: Literal["newest", "oldest"] | None,
        structured: bool = False,
        top_n: int | None = None,
    ) -> dict[str, Any]:
        # Only a supervisor/system_service page read can take its order from a
        # token, so only those two are given ``order`` as it came.
        chosen = order or "newest"
        numeric_offset = offset if isinstance(offset, int) else 0
        if source == "logbook":
            return await self._get_logbook(
                hours_back=hours_back if hours_back is not None else 1,
                entity_id=entity_id,
                device_id=device_id,
                context_id=context_id,
                end_time=end_time,
                limit=limit,
                offset=numeric_offset,
                search=search,
                compact=compact,
                order=chosen,
            )
        if source == "system":
            return await self._get_system_log(
                limit=limit, search=search, level=level, order=chosen, compact=compact
            )
        if source == "error_log":
            return await self._get_error_log(
                limit=limit,
                search=search,
                level=level,
                order=chosen,
                offset=offset,
                structured=structured,
                top_n=top_n,
            )
        if source == "logger":
            # logger reports per-integration levels, not time-ordered events;
            # 'order' does not apply (a warning is emitted upstream).
            return await self._get_logger_info(limit=limit, search=search)
        if source == "fault_log":
            return await self._get_fault_log(
                limit=limit, search=search, offset=numeric_offset, order=chosen
            )
        if source == "system_service":
            assert slug is not None  # guaranteed by _validate_log_slug
            return await self._get_system_service_log(
                service=slug,
                limit=limit,
                search=search,
                order=order,
                hours_back=hours_back,
                offset=offset,
            )
        assert slug is not None  # guaranteed by _validate_log_slug
        return await self._get_supervisor_log(
            slug=slug,
            limit=limit,
            search=search,
            order=order,
            hours_back=hours_back,
            offset=offset,
        )

    async def get_logs(
        self,
        source: str,
        limit: int | None,
        search: str | None,
        hours_back: int | None,
        entity_id: str | None,
        device_id: str | None,
        context_id: str | None,
        end_time: str | None,
        offset: int | str,
        compact: bool,
        level: str | None,
        slug: str | None,
        order: Literal["newest", "oldest"] | None = None,
        structured: bool = False,
        top_n: int | None = None,
    ) -> dict[str, Any]:
        level = _validate_log_level(level)
        offset = _validate_log_offset(source, offset)
        warnings = _collect_log_warnings(
            source,
            level,
            entity_id,
            device_id,
            context_id,
            end_time,
            slug,
            order or "newest",
            offset,
            hours_back=hours_back,
            search=search,
        )
        structured_error_log = structured and source == "error_log"
        if structured and source != "error_log":
            warnings.append(
                "Parameter 'structured' only applies to source='error_log'; "
                f"ignored for source='{source}'"
            )
        if top_n is not None and not structured_error_log:
            # Name the part that is actually missing. On source='error_log' the
            # source is already right and `structured` is the omission, so
            # blaming the source there contradicts the sentence's own opening.
            reason = (
                "ignored because structured=False"
                if source == "error_log"
                else f"ignored for source='{source}'"
            )
            warnings.append(
                "Parameter 'top_n' only applies to source='error_log' with "
                f"structured=True; {reason}"
            )
        _validate_log_slug(source, slug)
        result = await self._fetch_log_source(
            source,
            limit,
            search,
            hours_back,
            entity_id,
            device_id,
            context_id,
            end_time,
            offset,
            compact,
            level,
            slug,
            order,
            structured=structured_error_log,
            top_n=top_n,
        )
        if warnings:
            # Prepend, don't overwrite: the structured error_log path emits its
            # own warnings (format drift, ignored limit/order) and clobbering
            # them would drop the "this is NOT an all-clear" notice.
            result["warnings"] = warnings + result.get("warnings", [])
        return result


def register_logs_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register the Home Assistant log tool."""
    tools = LogTools(client)

    @mcp.tool(
        tags={"History & Statistics"},
        annotations=read_only_hints("Get Logs", open_world=False),
    )
    @log_tool_usage
    async def ha_get_logs(
        source: Annotated[
            Literal[
                "logbook",
                "system",
                "error_log",
                "supervisor",
                "system_service",
                "logger",
                "fault_log",
            ],
            Field(
                description=(
                    "'logbook': entity state-change history. 'system': HA's "
                    "structured system_log entries (errors, warnings) with full "
                    "exception tracebacks; search also matches those exceptions. "
                    "'error_log': raw log text (home-assistant.log on "
                    "container/pip installs; HA Core's journald stream on "
                    "Supervisor-backed installs), grouped into records: a header "
                    "line starts one and every following line that is not itself "
                    "a header joins it — traceback frames, the blank lines around "
                    "chained exceptions, and non-HA banners such as s6-overlay "
                    "restart lines. 'supervisor': app "
                    "(add-on) container logs (needs slug). 'system_service': "
                    "Supervisor-managed system service logs (needs slug). "
                    "'logger': effective log level per integration (confirms "
                    "ha_set_integration(log_level=...) changes took effect). 'fault_log': HA "
                    "Core's faulthandler crash dump (home-assistant.log.fault), "
                    "written only when HA dies from a native fatal signal, which "
                    "never reaches journald or error_log; empty on a healthy "
                    "install (crash_recorded=False); whole crash blocks are "
                    "ordered with each block's lines kept in place; reads "
                    "through the 'HA-MCP File & YAML Tools' entry."
                )
            ),
        ] = "logbook",
        # Shared parameters
        limit: Annotated[
            int | None,
            Field(
                description=(
                    "Max entries/lines to return. For source='error_log' with "
                    "structured=False it bounds returned lines: whole records "
                    "are selected within it, and a record longer than it is cut "
                    "(header and last lines kept) with a warning naming how many "
                    "lines were cut. Does not apply to source='error_log' with "
                    "structured=True. On a supervisor/system_service page read "
                    "(see offset) it counts journal entries, and an entry can "
                    "print several lines."
                )
            ),
        ] = None,
        search: Annotated[
            str | None,
            Field(
                description=(
                    "Keyword filter on entries/lines. For source='system' it "
                    "matches message, name and exception text; for the raw "
                    "error_log path it matches any line of a record, so a "
                    "traceback frame brings back its header and siblings. "
                    "Structured error_log mode matches message and logger name "
                    "only; source='logger' matches the integration domain."
                )
            ),
        ] = None,
        order: Annotated[
            Literal["newest", "oldest"] | None,
            Field(
                description=(
                    "Sort order for time-ordered sources (logbook, system, error_log, "
                    "supervisor, system_service, fault_log): 'newest' returns "
                    "most-recent first; 'oldest' returns chronological-first. For "
                    "error_log, supervisor and system_service it sets the read "
                    "direction of the newest lines read. A supervisor/"
                    "system_service page read (hours_back, no search) is "
                    "always oldest-first, so this may be omitted there; "
                    "'newest' is answered oldest-first with a warning. "
                    "Default 'newest'. "
                    "fault_log orders whole crash blocks; error_log orders "
                    "whole records and keeps each record's lines in place. Ignored "
                    "for source='logger', and for source='error_log' with "
                    "structured=True."
                )
            ),
        ] = None,
        # Source-scoped options: each description names the sources it applies to
        hours_back: Annotated[
            int | None,
            Field(
                ge=1,
                description=(
                    "How many hours back to read. Logbook (default 1), "
                    "supervisor and system_service. For the last two, only "
                    "when given: the journal serves that window, from its "
                    "start to the journal's end, one page at a time, "
                    "oldest-first (see offset); has_more says more of it remains. With search "
                    "the whole window is read at once, up to 10,000 entries: "
                    "window_complete says whether every entry of the window "
                    "was searched, and when false only the newest were."
                ),
            ),
        ] = None,
        entity_id: Annotated[
            str | None,
            Field(
                description=(
                    "Logbook only: restrict to one entity, or a comma-separated "
                    "list of entity IDs."
                )
            ),
        ] = None,
        device_id: Annotated[
            str | None,
            Field(
                description=(
                    "Logbook only: native Core device filter - events attributed "
                    "to this device and the context chains they caused. State "
                    "changes of the device's entities are not included; filter by "
                    "entity_id for those. Requires the WebSocket path."
                )
            ),
        ] = None,
        context_id: Annotated[
            str | None,
            Field(
                description=(
                    "Logbook only: only events sharing this context - 'what "
                    "caused this'. Cannot be combined with entity_id or "
                    "device_id; works on both transports when used alone."
                )
            ),
        ] = None,
        end_time: Annotated[
            str | None,
            Field(description="Logbook only: end of the window (ISO datetime)."),
        ] = None,
        offset: Annotated[
            int | str,
            Field(
                description=(
                    "Page deeper into source='logbook', 'error_log', "
                    "'fault_log', and supervisor/system_service on a page "
                    "read: hours_back and no search (ignored otherwise). "
                    "There it is the opaque token a previous page returned "
                    "in 'next_offset', which continues from an exact journal "
                    "entry in the same window; pass it with the same slug "
                    "(hours_back may be omitted). An integer skips that many "
                    "entries from the window's first. A page's total_lines is its line count. On error_log it "
                    "counts raw log lines back from the newest entry, or is the "
                    "opaque token a previous page returned in 'next_offset' on "
                    "Supervisor/journald installs, which keeps paging exact "
                    "while new log lines arrive (`structured=True` accepts and "
                    "returns that token too); on fault_log it counts lines "
                    "from the start of the assembled crash text. Pass the "
                    "response's 'next_offset' to continue while 'has_more' is "
                    "true."
                ),
            ),
        ] = 0,
        compact: Annotated[
            bool,
            Field(
                description=(
                    f"logbook / system only: save context. Logbook strips "
                    f"attribute dicts; system keeps the start and end of each "
                    f"message string within {_SYSTEM_LOG_MESSAGE_CAP} characters "
                    f"(counted in 'truncated_messages'). search still matches the "
                    f"full message; text inside the cut is not shown. To read one "
                    f"message whole, pass False with a narrow search and limit=1."
                )
            ),
        ] = True,
        # System/error_log-specific
        level: Annotated[
            str | None,
            Field(
                description=(
                    "system / error_log only: keep only entries at exactly this level "
                    "(ERROR, WARNING, INFO, DEBUG, CRITICAL); it is not a threshold. "
                    "For source='error_log' it matches the record's header only, so "
                    "matching text in a traceback frame does not satisfy it."
                )
            ),
        ] = None,
        # error_log-specific: structured summary instead of raw text
        structured: Annotated[
            bool,
            Field(
                description=(
                    "source='error_log' only. When True, return a deduplicated, "
                    "component-grouped summary of the log (counted issues sorted by "
                    "frequency) instead of raw text. Ignored for other sources."
                )
            ),
        ] = False,
        top_n: Annotated[
            int | None,
            Field(
                ge=1,
                description=(
                    f"Max distinct issues to return when structured=True "
                    f"(default {_DEFAULT_TOP_N}, capped at {MAX_LIMIT})."
                ),
            ),
        ] = None,
        # Supervisor + system_service-specific (different namespaces)
        slug: Annotated[
            str | None,
            Field(
                description=(
                    "source='supervisor': app slug, e.g. 'core_mosquitto' (use "
                    "ha_get_app() to list installed slugs). "
                    "source='system_service': service name, one of supervisor, "
                    "host, core, dns, audio, multicast — here "
                    "'supervisor' is the Supervisor service's own logs, not an "
                    "app with that name."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Get Home Assistant logs from various sources.

        Prefer source='system' for triage: it returns HA's own deduplicated
        system_log entries with counts, first_occurred and the full exception
        traceback, and its counts run since each error first occurred. error_log with
        structured=True counts only what is inside the fetched window (reported
        as window_start/window_end; every install reads a capped window) and
        drops tracebacks; structured=False returns the raw records instead, so
        it covers what falls outside system_log's WARNING+ in-memory buffer. Use
        structured=True for the per-component rollup; in structured mode
        limit/order do not apply: issues are ranked by count, then severity,
        then recency, over a fixed deep window.

        Raw-text sources (error_log, supervisor, system_service) read a bounded
        window per call, so level/search filter and limit slice within that
        window only; window_lines reports the size requested. With hours_back,
        supervisor and system_service read the journal's own time window
        instead. Logbook and hours_back responses carry has_more plus a
        pagination_hint; error_log, fault_log and supervisor/system_service
        page reads carry has_more with a next_offset to pass back while it
        stays true.
        """
        return await tools.get_logs(
            source=source,
            limit=limit,
            search=search,
            hours_back=hours_back,
            entity_id=entity_id,
            device_id=device_id,
            context_id=context_id,
            end_time=end_time,
            offset=offset,
            compact=compact,
            level=level,
            slug=slug,
            order=order,
            structured=structured,
            top_n=top_n,
        )
