"""``ha_get_logs`` — one tool over every Home Assistant log source.

Holds the source dispatch, the cross-source parameter rules, and the tool
registration. The sources themselves are mixed in from ``log_sources`` (Core)
and ``log_sources_supervisor`` (Supervisor), their shared plumbing lives in
``log_common``, and the error-log window arithmetic in ``error_log_parsing``.

Split out of ``tools_utility`` under `.gemini/styleguide.md` § Tool
Consolidation and Module Size.
"""

from typing import Annotated, Any, Literal

from pydantic import Field

from .error_log_parsing import _DEFAULT_TOP_N
from .helpers import log_tool_usage
from .log_common import (
    MAX_LIMIT,
    _collect_log_warnings,
    _validate_log_level,
    _validate_log_slug,
)
from .log_sources import CoreLogSourcesMixin
from .log_sources_fault import FaultLogSourceMixin
from .log_sources_supervisor import SupervisorLogSourcesMixin


class LogTools(CoreLogSourcesMixin, SupervisorLogSourcesMixin, FaultLogSourceMixin):
    """Dispatches ``ha_get_logs`` to one log source and shapes the response."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _fetch_log_source(
        self,
        source: str,
        limit: int | None,
        search: str | None,
        hours_back: int,
        entity_id: str | None,
        end_time: str | None,
        offset: int,
        compact: bool,
        level: str | None,
        slug: str | None,
        order: Literal["newest", "oldest"],
        structured: bool = False,
        top_n: int | None = None,
    ) -> dict[str, Any]:
        if source == "logbook":
            return await self._get_logbook(
                hours_back=hours_back,
                entity_id=entity_id,
                end_time=end_time,
                limit=limit,
                offset=offset,
                search=search,
                compact=compact,
                order=order,
            )
        if source == "system":
            return await self._get_system_log(
                limit=limit, search=search, level=level, order=order
            )
        if source == "error_log":
            return await self._get_error_log(
                limit=limit,
                search=search,
                level=level,
                order=order,
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
                limit=limit, search=search, offset=offset, order=order
            )
        if source == "system_service":
            assert slug is not None  # guaranteed by _validate_log_slug
            return await self._get_system_service_log(
                service=slug, limit=limit, search=search, order=order
            )
        assert slug is not None  # guaranteed by _validate_log_slug
        return await self._get_supervisor_log(
            slug=slug, limit=limit, search=search, order=order
        )

    async def get_logs(
        self,
        source: str,
        limit: int | None,
        search: str | None,
        hours_back: int,
        entity_id: str | None,
        end_time: str | None,
        offset: int,
        compact: bool,
        level: str | None,
        slug: str | None,
        order: Literal["newest", "oldest"] = "newest",
        structured: bool = False,
        top_n: int | None = None,
    ) -> dict[str, Any]:
        level = _validate_log_level(level)
        warnings = _collect_log_warnings(
            source, level, entity_id, end_time, slug, order, offset
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
        annotations={
            "openWorldHint": False,
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get Logs",
        },
    )
    @log_tool_usage
    async def ha_get_logs(
        source: Literal[
            "logbook",
            "system",
            "error_log",
            "supervisor",
            "system_service",
            "logger",
            "fault_log",
        ] = "logbook",
        # Shared parameters
        limit: int | None = None,
        search: str | None = None,
        order: Annotated[
            Literal["newest", "oldest"],
            Field(
                description=(
                    "Sort order for time-ordered sources (logbook, system, "
                    "error_log, supervisor, system_service, fault_log): "
                    "'newest' (default) "
                    "returns most-recent first; 'oldest' returns chronological-"
                    "first. Ignored for source='logger', and for "
                    "source='error_log' with structured=True (that summary is "
                    "ranked by occurrence count, not by time)."
                )
            ),
        ] = "newest",
        # Logbook-specific (ignored for other sources)
        hours_back: Annotated[int, Field(ge=1)] = 1,
        entity_id: str | None = None,
        end_time: str | None = None,
        offset: Annotated[
            int,
            Field(
                ge=0,
                description=(
                    "Page deeper into source='logbook', 'error_log' and "
                    "'fault_log' (ignored for other sources). On error_log it "
                    "counts raw log lines back from the newest entry; on "
                    "fault_log it counts lines from the start of the assembled "
                    "crash text. Pass the response's 'next_offset' to continue "
                    "while 'has_more' is true."
                ),
            ),
        ] = 0,
        compact: bool = True,
        # System/error_log-specific
        level: str | None = None,
        # error_log-specific: structured summary instead of raw text
        structured: Annotated[
            bool,
            Field(
                description=(
                    "source='error_log' only. When True, return a deduplicated, "
                    "component-grouped summary of the log (counted issues sorted "
                    "by frequency) instead of raw text. Use this on busy "
                    "instances where the raw log is large enough to exhaust "
                    "context. Ignored for other sources."
                )
            ),
        ] = False,
        top_n: Annotated[
            int | None,
            Field(
                ge=1,
                description=(
                    f"Max distinct issues to return when structured=True "
                    f"(default {_DEFAULT_TOP_N}, capped at {MAX_LIMIT}). Bounds "
                    "the response regardless of log size."
                ),
            ),
        ] = None,
        # Supervisor + system_service-specific (different namespaces)
        slug: str | None = None,
    ) -> dict[str, Any]:
        """
        Get Home Assistant logs from various sources.

        **Sources:**
        - "logbook" (default): Entity state change history with pagination
        - "system": Structured system log entries (errors, warnings) via system_log/list
        - "error_log": Raw log text (home-assistant.log on container/pip installs; HA Core's journald stream on Supervisor-backed installs)
        - "supervisor": App (add-on) container logs (requires slug = app slug)
        - "system_service": HA-Supervisor-managed system service logs (requires
          slug ∈ {supervisor, host, core, dns, audio, cli, multicast, observer})
        - "logger": Effective log level per integration via logger/log_info (confirms logger.set_level changes took effect)
        - "fault_log": HA Core's faulthandler crash dump (home-assistant.log.fault).
          Written only when HA dies from a native fatal signal (segfault, abort,
          Python fatal error), which never reaches journald or error_log. Empty
          on a healthy install (crash_recorded=False). Whole crash blocks are
          ordered (newest first by default) with each block's lines kept in
          place so the traceback reads correctly; search keeps every block
          that mentions the term; offset/limit page through the assembled
          text. Reads through the "HA-MCP File & YAML Tools"
          entry (component >= 2.1.4).

        **Prefer source='system' for triage.** It returns HA's own deduplicated
        system_log entries with counts, first_occurred and full tracebacks; of
        those only the tracebacks are unrecoverable from the structured
        error_log summary — they are present in the raw text, so structured=False
        gets them back. Its counts also run
        since each error first occurred, while structured error_log counts only
        what is inside the fetched window (reported as window_start/window_end;
        every install now reads a capped window). Use error_log
        with structured=True for entries below system_log's WARNING+ ~50-entry
        cap, or for the per-component rollup.

        **Shared params:** limit, search (keyword filter on entries/lines; matches integration domain for source='logger')
        **Order:** order='newest' (default) returns most-recent first; order='oldest' returns chronological-first. Applies to all time-ordered sources (logbook, system, error_log, supervisor, system_service, fault_log); ignored for source='logger' and for error_log with structured=True. For raw-text sources (error_log, supervisor, system_service) it sets the read direction of the most-recent window; fault_log orders whole crash blocks instead of lines.
        **Logbook params:** hours_back, entity_id, end_time, compact (default True — strips attribute dicts to save context)
        **Pagination (logbook + error_log + fault_log):** offset pages deeper; ignored for the
            other sources. fault_log always reads a fixed window from the end of
            the file, orders its crash blocks, and pages the assembled text
            from the start with has_more/next_offset. Logbook responses carry has_more plus a
            pagination_hint. On error_log, offset counts raw log lines back from
            the newest entry (journald entries on Supervisor-backed installs),
            both modes read a bounded window per call — so `level`/`search`
            filter and `limit` slice within that window only, and window_lines
            reports the size actually requested — and the response carries
            has_more with a next_offset to pass back while it stays true.
        **System/error_log params:** level (ERROR, WARNING, INFO, DEBUG, CRITICAL)
        **error_log params:** structured, top_n. In structured mode `search`
            matches the message and logger name only, whereas on the raw path it
            matches the whole line; `limit`/`order` do not apply, issues are
            ranked by count, then severity, then recency, and the summary covers
            a fixed deep window rather than the caller's limit.
        **Supervisor params:** slug = app slug, e.g. "core_mosquitto" (use
            ha_get_app() to list installed slugs)
        **System-service params:** slug = service name. The slug "supervisor"
            here means the Supervisor service's own logs, NOT an app with
            that name — the source param disambiguates.
        """
        return await tools.get_logs(
            source=source,
            limit=limit,
            search=search,
            hours_back=hours_back,
            entity_id=entity_id,
            end_time=end_time,
            offset=offset,
            compact=compact,
            level=level,
            slug=slug,
            order=order,
            structured=structured,
            top_n=top_n,
        )
