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
from ..errors import TOOL_ERROR_LOG_LEVEL, ErrorCode, create_error_response
from .coercion import JSON_STRING_COERCION
from .error_log_parsing import _DEFAULT_TOP_N
from .helpers import log_tool_usage, raise_tool_error
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
from .logbook_cursor import LOGBOOK_CURSOR_PREFIX, LogbookCursor
from .supervisor_log_reader import exact_log_window, selected_journal
from .tool_hints import read_only_hints


def _validate_offset_token(source: str, token: str) -> str:
    """Check ``token`` is a ``next_offset`` value ``source`` itself returns."""
    if source == "logbook" and token.startswith(LOGBOOK_CURSOR_PREFIX):
        parse: Callable[[str], object] = LogbookCursor.parse
    elif source in ("supervisor", "system_service"):
        parse = parse_window_token
    elif source == "error_log":
        if not is_cursor_token(token):
            raise ToolError(
                "Invalid 'offset' token for source='error_log'; pass the "
                "'next_offset' value a previous response returned",
                log_level=TOOL_ERROR_LOG_LEVEL,
            )
        parse = parse_cursor_token
    elif source == "logbook":
        raise ToolError(
            "Invalid 'offset' token for source='logbook'; pass an integer or "
            "the 'next_offset' value a previous response returned",
            log_level=TOOL_ERROR_LOG_LEVEL,
        )
    else:
        raise ToolError(
            "Cursor tokens only apply to source='error_log', 'supervisor', "
            "'system_service' and 'logbook'; "
            f"source='{source}' takes a non-negative integer offset",
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


def _with_warnings(result: dict[str, Any], *groups: list[str]) -> dict[str, Any]:
    """Put ``groups`` of warnings ahead of the ones ``result`` already carries."""
    warnings = [warning for group in groups for warning in group]
    if warnings:
        result["warnings"] = warnings + result.get("warnings", [])
    return result


def _unused(**given: object) -> list[str]:
    """A warning naming the parameters given that this read does not use."""
    names = [name for name, value in given.items() if value]
    if not names:
        return []
    return [f"Parameters {', '.join(names)} do not apply to this read; ignored."]


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
        action_calls: bool = False,
        integration: str | None = None,
        start_time: str | None = None,
    ) -> dict[str, Any]:
        # Only a supervisor/system_service page read can take its order from a
        # token, so only those two are given ``order`` as it came.
        chosen = order or "newest"
        numeric_offset = offset if isinstance(offset, int) else 0
        if source == "logbook":
            return await self._get_logbook(
                hours_back=hours_back,
                entity_id=entity_id,
                device_id=device_id,
                context_id=context_id,
                end_time=end_time,
                limit=limit,
                offset=offset,
                search=search,
                compact=compact,
                order=chosen,
                action_calls=action_calls,
                start_time=start_time,
            )
        if source == "system":
            return await self._get_system_log(
                limit=limit,
                search=search,
                level=level,
                order=chosen,
                compact=compact,
                integration=integration,
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
                integration=integration,
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
        action_calls: bool = False,
        integration: str | None = None,
        boot: int | str | None = None,
        identifier: str | None = None,
        latest: bool = False,
        listing: str | None = None,
        start_time: str | None = None,
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        level = _validate_log_level(level)
        offset = _validate_log_offset(source, offset)
        unused_here = {
            "entity_id": entity_id,
            "device_id": device_id,
            "context_id": context_id,
            "action_calls": action_calls,
            "top_n": top_n,
        }
        # Logbook has its own window; every other start_time is an exact window.
        if sources is not None or (start_time is not None and source != "logbook"):
            if any(
                (
                    hours_back,
                    level,
                    structured,
                    integration,
                    boot is not None,
                    identifier,
                    latest,
                    listing,
                )
            ):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "An exact window takes start_time, end_time, search and limit, "
                        "with sources for a timeline or offset for one raw source.",
                    )
                )
            return _with_warnings(
                await exact_log_window(
                    self._client,
                    source,
                    slug,
                    sources,
                    start_time,
                    end_time,
                    limit,
                    offset,
                    search,
                    order,
                ),
                _unused(**unused_here, slug=sources is not None and slug),
            )
        if boot is not None or identifier or latest or listing:
            if hours_back or offset or integration or level or structured:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "A journal selection (boot, latest, identifier, listing) "
                        "takes limit, search and order.",
                    )
                )
            return _with_warnings(
                await selected_journal(
                    self._client,
                    source,
                    slug,
                    boot=boot,
                    identifier=identifier,
                    latest=latest,
                    listing=listing,
                    limit=limit,
                    search=search,
                    order=order,
                ),
                _unused(**unused_here, end_time=end_time),
            )
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
            action_calls=action_calls,
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
        if integration and source not in {"error_log", "system"}:
            warnings.append(
                f"Parameter 'integration' applies to system and error_log; ignored for source='{source}'."
            )
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
            action_calls=action_calls,
            integration=integration,
            start_time=start_time,
        )
        # Prepended, not overwritten: the structured error_log path emits its
        # own warnings (format drift, ignored limit/order) and clobbering
        # them would drop the "this is NOT an all-clear" notice.
        return _with_warnings(result, warnings)


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
                    "'logbook': entity state-change history, or the actions "
                    "called with action_calls. 'system': HA's "
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
                    "through either HA-MCP component entry."
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
                    "print several lines. A timeline (sources) counts records and "
                    "keeps the records of one instant together, so it can return "
                    "a few more."
                )
            ),
        ] = None,
        search: Annotated[
            str | None,
            Field(
                description=(
                    "Keyword filter on entries/lines. Logbook searches the displayed "
                    "name and message together (e.g. 'Home Assistant started'), "
                    "and the entity ID; an action call matches on domain.service. "
                    "For source='system' it "
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
                    "'newest' is answered oldest-first with a warning, as is an "
                    "exact window or timeline (start_time). "
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
                    "supervisor and system_service. Without a current component, HA-MCP caps Logbook reads at 24 hours. "
                    "For the journal sources, only "
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
            Field(
                description=(
                    "End of the window, ISO 8601. Logbook: defaults to now, and "
                    "a time without an offset is Home Assistant's local time. "
                    "The other sources need it with start_time, both with a "
                    "UTC offset."
                )
            ),
        ] = None,
        offset: Annotated[
            int | str,
            Field(
                description=(
                    "For a Logbook cursor, pass next_offset as offset with the same filters, "
                    "search and order. HA-MCP caps recorder pages per call. When total_entries is null, "
                    "total_entries_lower_bound counts verified matches, and a page can come back short or empty with has_more true: keep following next_offset. For integer offsets reuse hours_back and end_time. "
                    "Page deeper into source='logbook', 'error_log', "
                    "'fault_log', and supervisor/system_service on a page "
                    "read: hours_back and no search (ignored otherwise). "
                    "There it is the opaque token a previous page returned "
                    "in 'next_offset', which continues from an exact journal "
                    "entry in the same window; pass it with the same slug "
                    "(hours_back may be omitted). An integer skips that many "
                    "entries from the window's first. A page's total_lines is its line count. On error_log it "
                    "counts raw log lines back from the newest entry, or is the "
                    "opaque token a previous page returned in 'next_offset', "
                    "which keeps paging exact "
                    "while new log lines arrive (`structured=True` accepts and "
                    "returns that token too); with integration, and in an exact "
                    "raw window (start_time), it is an integer; on fault_log it "
                    "counts lines "
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
                    "Logbook returns every native field in either mode. For system logs, "
                    f"compact mode keeps the start and end of each "
                    f"message string within {_SYSTEM_LOG_MESSAGE_CAP} characters "
                    f"(counted in 'truncated_messages'). search still matches the "
                    f"full message; text inside the cut is not shown. To read one "
                    f"message whole, pass False with a narrow search and limit=1."
                )
            ),
        ] = True,
        action_calls: Annotated[
            bool,
            Field(
                description="Logbook only. Read admin-only action calls and their service data for the time window. Omit entity, device and context filters."
            ),
        ] = False,
        # System/error_log-specific
        integration: Annotated[
            str | None,
            Field(
                description="Filter system entries or the raw Core log by integration domain, including its library loggers."
            ),
        ] = None,
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
                    "source='error_log' only. When True, return a deduplicated "
                    "summary of the log grouped by integration (counted issues "
                    "sorted by frequency) instead of raw text. Ignored for other "
                    "sources."
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
        # Exact windows and journal selections
        start_time: Annotated[
            str | None,
            Field(
                description=(
                    "Start of an exact window, ISO 8601 with offset, for "
                    "error_log, supervisor, system_service or sources. Logbook: "
                    "the window's start, in place of hours_back."
                )
            ),
        ] = None,
        sources: Annotated[
            list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Up to twelve timeline sources: core, supervisor, host, a "
                    "plugin name or app:<slug>. The journal ones need a "
                    "Supervisor install. A source that cannot be read is named "
                    "in 'errors' and the others are returned."
                )
            ),
        ] = None,
        boot: Annotated[
            int | str | None,
            Field(
                description=(
                    "Journal boot for error_log, supervisor or system_service: "
                    "an offset (0 current, -1 previous) or a boot ID from "
                    "listing='boots'."
                )
            ),
        ] = None,
        identifier: Annotated[
            str | None,
            Field(
                description=(
                    "Syslog identifier in the host journal (source="
                    "'system_service', slug='host'), from listing='identifiers'."
                )
            ),
        ] = None,
        latest: Annotated[
            bool,
            Field(
                description=(
                    "Read since the app or service container last started "
                    "(error_log, supervisor, system_service; not the host)."
                )
            ),
        ] = False,
        listing: Annotated[
            Literal["boots", "identifiers"] | None,
            Field(
                description=(
                    "List the host journal's boots or syslog identifiers "
                    "instead of reading lines (Supervisor installs)."
                )
            ),
        ] = None,
        slug: Annotated[
            str | None,
            Field(
                description=(
                    "source='supervisor': app slug, e.g. 'core_mosquitto' (use "
                    "ha_get_app() to list installed slugs). "
                    "source='system_service': a Supervisor service name, e.g. host or core. Here "
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
        structured=True for the per-integration rollup; in structured mode
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

        Exact windows: start_time and end_time with a source return that
        source's lines in the window. With sources they return one merged
        timeline of records instead; pass next_start_time back as start_time
        to continue. boot, latest, identifier and listing take only limit,
        search and order.

        EXAMPLES:
        - Timeline: ha_get_logs(sources=["core", "supervisor"],
          start_time="2026-01-05T10:00:00+00:00", end_time="2026-01-05T10:05:00+00:00")
        - Core log of the previous boot: ha_get_logs(source="error_log", boot=-1)
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
            action_calls=action_calls,
            integration=integration,
            boot=boot,
            identifier=identifier,
            latest=latest,
            listing=listing,
            start_time=start_time,
            sources=sources,
        )
