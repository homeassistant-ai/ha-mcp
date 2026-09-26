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
                    "structured system_log entries (errors, warnings). "
                    "'error_log': raw log text (home-assistant.log on "
                    "container/pip installs; HA Core's journald stream on "
                    "Supervisor-backed installs). 'supervisor': app (add-on) "
                    "container logs (needs slug). 'system_service': "
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
                    "Max entries/lines to return. Does not apply to "
                    "source='error_log' with structured=True."
                )
            ),
        ] = None,
        search: Annotated[
            str | None,
            Field(
                description=(
                    "Keyword filter on entries/lines; matches the integration "
                    "domain for source='logger'. In structured error_log mode it "
                    "matches the message and logger name only; on the raw path "
                    "the whole line."
                )
            ),
        ] = None,
        order: Annotated[
            Literal["newest", "oldest"],
            Field(
                description=(
                    "Sort order for time-ordered sources (logbook, system, error_log, "
                    "supervisor, system_service, fault_log): 'newest' returns "
                    "most-recent first; 'oldest' returns chronological-first. For "
                    "raw-text sources it sets the read direction of the most-recent "
                    "window; fault_log orders whole crash blocks. Ignored for "
                    "source='logger', and for source='error_log' with structured=True."
                )
            ),
        ] = "newest",
        # Logbook-specific (ignored for other sources)
        hours_back: Annotated[
            int, Field(ge=1, description="Logbook only: how many hours back to read.")
        ] = 1,
        entity_id: Annotated[
            str | None, Field(description="Logbook only: restrict to this entity.")
        ] = None,
        end_time: Annotated[
            str | None,
            Field(description="Logbook only: end of the window (ISO datetime)."),
        ] = None,
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
        compact: Annotated[
            bool,
            Field(description="Logbook only: strip attribute dicts to save context."),
        ] = True,
        # System/error_log-specific
        level: Annotated[
            str | None,
            Field(
                description=(
                    "system / error_log only: keep only entries at exactly this level "
                    "(ERROR, WARNING, INFO, DEBUG, CRITICAL); it is not a threshold."
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
                    "host, core, dns, audio, cli, multicast, observer — here "
                    "'supervisor' is the Supervisor service's own logs, not an "
                    "app with that name."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Get Home Assistant logs from various sources.

        Prefer source='system' for triage: it returns HA's own deduplicated
        system_log entries with counts, first_occurred and full tracebacks, and
        its counts run since each error first occurred. error_log with
        structured=True counts only what is inside the fetched window (reported
        as window_start/window_end; every install reads a capped window) and
        drops tracebacks, which structured=False gets back; use it for entries
        below system_log's WARNING+ ~50-entry cap, or for the per-component
        rollup. In structured mode limit/order do not apply: issues are ranked
        by count, then severity, then recency, over a fixed deep window.

        Raw-text sources (error_log, supervisor, system_service) read a bounded
        window per call, so level/search filter and limit slice within that
        window only; window_lines reports the size requested. Logbook responses
        carry has_more plus a pagination_hint; error_log and fault_log carry
        has_more with a next_offset to pass back while it stays true.
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
