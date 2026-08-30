"""Log sources served by ``ha_get_logs`` from Home Assistant itself.

Logbook, ``system_log``, the raw/structured error log, and the per-integration
logger levels. Mixed into ``LogTools`` (``tools_logs``) rather than standing
alone: every fetcher here is a thin shell around one client call, and the
dispatch, validation and tool registration they share live with the host class.
The Supervisor-backed sources are in ``log_sources_supervisor``.

Split out of ``tools_utility`` under .gemini/styleguide.md § Tool Consolidation and Module Size.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastmcp.exceptions import ToolError

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from .error_log_parsing import (
    _DEFAULT_TOP_N,
    _EMPTY_FETCH_WARNING,
    _attach_error_log_pagination,
    _error_log_window_lines,
    _ErrorLogWindow,
    _next_page_step,
    _parse_error_log_structured,
)
from .helpers import exception_to_structured_error, raise_tool_error
from .log_common import (
    _LOG_LEVEL_RE,
    DEFAULT_LOG_LIMIT,
    _addon_auth_error_suggestions,
    _coerce_limit,
    _compact_logbook_entries,
)
from .util_helpers import add_timezone_metadata, normalize_log_level

logger = logging.getLogger(__name__)


class CoreLogSourcesMixin:
    """The log sources HA Core serves directly.

    ``_client`` is supplied by the host class (``LogTools``); this mixin adds
    no state of its own.
    """

    _client: Any

    @staticmethod
    def _coerce_logbook_params(
        hours_back: int,
        limit: int | None,
        offset: int,
    ) -> tuple[int, int, int]:
        effective_limit = _coerce_limit(limit)
        return hours_back, effective_limit, offset

    @staticmethod
    def _build_pagination_hint(
        offset_int: int,
        effective_limit: int,
        total_entries: int,
        paginated_entries: Any,
        hours_back_int: int,
        end_time: str | None,
        entity_id: str | None,
        search: str | None,
        compact_bool: bool,
        order: Literal["newest", "oldest"] = "newest",
    ) -> str:
        """Build reproducible pagination hint string for logbook results.

        String values are quoted: the hint is meant to be copied back as a
        call, and an unquoted ``search=front door`` is not one.
        """
        next_offset = offset_int + effective_limit
        param_parts = [
            f"hours_back={hours_back_int}",
            f"limit={effective_limit}",
            f"offset={next_offset}",
        ]
        if entity_id:
            param_parts.append(f"entity_id={entity_id!r}")
        if end_time:
            param_parts.append(f"end_time={end_time!r}")
        if search:
            param_parts.append(f"search={search!r}")
        if not compact_bool:
            param_parts.append("compact=False")
        if order != "newest":
            param_parts.append(f"order={order!r}")
        param_str = ", ".join(param_parts)
        return (
            f"Showing entries {offset_int + 1}-{offset_int + len(paginated_entries)} of {total_entries}. "
            f"To get the next page, use: ha_get_logs({param_str})"
        )

    @staticmethod
    def _filter_logbook_by_search(
        response: Any, search: str | None
    ) -> tuple[Any, dict[str, str]]:
        """Filter logbook entries by search term across name/message/entity_id.

        Returns the (possibly filtered) response and a filters_applied dict
        recording which filters were used.
        """
        filters_applied: dict[str, str] = {}
        if search and isinstance(response, list):
            search_lower = search.lower()
            response = [
                e
                for e in response
                if isinstance(e, dict)
                and (
                    search_lower in str(e.get("name", "")).lower()
                    or search_lower in str(e.get("message", "")).lower()
                    or search_lower in str(e.get("entity_id", "")).lower()
                )
            ]
            filters_applied["search"] = search
        return response, filters_applied

    @staticmethod
    def _paginate_logbook_entries(
        response: Any,
        offset_int: int,
        effective_limit: int,
        order: Literal["newest", "oldest"],
    ) -> tuple[Any, int, bool]:
        """Slice logbook entries into the requested page.

        HA's /logbook returns entries oldest-first. Takes a window from
        the end for newest-first (default), or from the start for
        oldest-first, with offset paging deeper in the chosen order.
        """
        total_entries = len(response) if isinstance(response, list) else 1

        if isinstance(response, list):
            if order == "newest":
                end = total_entries - offset_int
                start = max(end - effective_limit, 0)
                paginated_entries = (
                    list(reversed(response[start:end])) if end > 0 else []
                )
            else:
                paginated_entries = response[offset_int : offset_int + effective_limit]
            has_more = offset_int + len(paginated_entries) < total_entries
        else:
            paginated_entries = response
            has_more = False

        return paginated_entries, total_entries, has_more

    @staticmethod
    def _logbook_error_suggestions(error_str: str) -> list[str]:
        """Build remediation suggestions for a logbook fetch failure.

        Adds server-crash-specific guidance when the error indicates a 500
        (heavy query causing HA to fail) on top of the general tips.
        """
        if "500" in error_str:
            return [
                "The query returned too many results causing a server error (500).",
                "This often happens with very active entities or long time periods.",
                "Try reducing 'hours_back' parameter (e.g., from 24 to 1 hour)",
                "Add a specific 'entity_id' filter to narrow down results",
                "If debugging an automation, filter by that automation's entity_id",
                "Use ha_report_issue to check Home Assistant logs for crash details",
            ]
        return [
            "Try reducing 'hours_back' parameter (e.g., from 24 to 1 hour)",
            "Add a specific 'entity_id' filter to narrow down results",
        ]

    async def _get_logbook(
        self,
        hours_back: int = 1,
        entity_id: str | None = None,
        end_time: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        search: str | None = None,
        compact: bool = True,
        order: Literal["newest", "oldest"] = "newest",
    ) -> dict[str, Any]:
        """Fetch logbook entries with search and pagination."""
        hours_back_int, effective_limit, offset_int = self._coerce_logbook_params(
            hours_back, limit, offset
        )

        if end_time:
            try:
                end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            except ValueError:
                # Outside the fetch try-block below, so without this guard the
                # raw ValueError would bypass the structured ToolError shape.
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid end_time '{end_time}': not an ISO 8601 timestamp",
                        suggestions=[
                            "Use ISO format, e.g. end_time='2026-08-28T01:00:00Z'"
                        ],
                    )
                )
        else:
            end_dt = datetime.now(UTC)

        start_dt = end_dt - timedelta(hours=hours_back_int)
        start_timestamp = start_dt.isoformat()

        try:
            response = await self._client.get_logbook(
                entity_id=entity_id, start_time=start_timestamp, end_time=end_time
            )

            response, filters_applied = self._filter_logbook_by_search(response, search)

            paginated_entries, total_entries, has_more = self._paginate_logbook_entries(
                response, offset_int, effective_limit, order
            )

            # In compact mode, strip entries to essential fields only.
            # This prevents full attribute dictionaries from exhausting
            # the LLM context window during debugging workflows.
            if compact and isinstance(paginated_entries, list):
                paginated_entries = _compact_logbook_entries(paginated_entries)

            logbook_data: dict[str, Any] = {
                "success": True,
                "source": "logbook",
                "entries": paginated_entries,
                "period": f"{hours_back_int} hours back from {end_dt.isoformat()}",
                "start_time": start_timestamp,
                "end_time": end_dt.isoformat(),
                "entity_filter": entity_id,
                "total_entries": total_entries,
                "returned_entries": len(paginated_entries)
                if isinstance(paginated_entries, list)
                else 1,
                "limit": effective_limit,
                "offset": offset_int,
                "order": order,
                "has_more": has_more,
            }
            if filters_applied:
                logbook_data["filters_applied"] = filters_applied
            if has_more:
                logbook_data["pagination_hint"] = self._build_pagination_hint(
                    offset_int,
                    effective_limit,
                    total_entries,
                    paginated_entries,
                    hours_back_int,
                    end_time,
                    entity_id,
                    search,
                    compact,
                    order,
                )

            return await add_timezone_metadata(self._client, logbook_data)

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={
                    "period": f"{hours_back_int} hours back from {end_dt.isoformat()}",
                },
                suggestions=self._logbook_error_suggestions(str(e)),
            )
            raise  # unreachable: exception_to_structured_error always raises

    @staticmethod
    def _system_log_sort_key(entry: Any) -> float:
        """Total-order-safe sort key for system_log entries.

        ``system_log/list`` does not guarantee a numeric ``timestamp`` on every
        record. Coerce a missing / non-numeric / non-dict entry to ``0.0`` so
        sorting never raises a cross-type ``TypeError`` (bools are excluded so
        a stray ``True`` doesn't read as ``1.0``).
        """
        if not isinstance(entry, dict):
            return 0.0
        ts = entry.get("timestamp")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            return 0.0
        return float(ts)

    async def _get_system_log(
        self,
        limit: int | None = None,
        search: str | None = None,
        level: str | None = None,
        order: Literal["newest", "oldest"] = "newest",
    ) -> dict[str, Any]:
        """Fetch structured system log entries via system_log/list."""
        effective_limit = _coerce_limit(limit)

        try:
            result = await self._client.send_websocket_message(
                {"type": "system_log/list"}
            )

            if not result.get("success"):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        result.get("error", "Failed to retrieve system log"),
                        suggestions=["Check Home Assistant connection"],
                    )
                )

            entries = result.get("result", [])
            if not isinstance(entries, list):
                entries = []

            filters_applied: dict[str, str] = {}

            # The isinstance guards keep a malformed non-dict record from
            # raising AttributeError out of this method's narrow except
            # clauses without the structured ToolError envelope. Unfiltered
            # calls still return such records verbatim (pinned by
            # test_tolerates_missing_none_and_non_dict_entries); a filter
            # drops them, since they cannot carry the field being matched.
            if level:
                entries = [
                    e
                    for e in entries
                    if isinstance(e, dict) and str(e.get("level", "")).upper() == level
                ]
                filters_applied["level"] = level

            if search:
                search_lower = search.lower()
                entries = [
                    e
                    for e in entries
                    if isinstance(e, dict)
                    and (
                        search_lower in str(e.get("message", "")).lower()
                        or search_lower in str(e.get("name", "")).lower()
                    )
                ]
                filters_applied["search"] = search

            # system_log/list entries carry a 'timestamp' (epoch float, last
            # occurrence), but HA does not guarantee it on every record. Sort
            # with a total-order-safe key so 'order' is deterministic regardless
            # of HA's native ordering (newest-first by default) and a missing /
            # non-numeric / non-dict entry can never raise a cross-type
            # TypeError out of this method's narrow except clause.
            entries.sort(
                key=self._system_log_sort_key,
                reverse=(order == "newest"),
            )

            total_entries = len(entries)
            entries = entries[:effective_limit]

            data: dict[str, Any] = {
                "success": True,
                "source": "system",
                "entries": entries,
                "total_entries": total_entries,
                "returned_entries": len(entries),
                "limit": effective_limit,
                "order": order,
            }
            if filters_applied:
                data["filters_applied"] = filters_applied

            return data

        except ToolError:
            raise
        except (
            HomeAssistantConnectionError,
            HomeAssistantAPIError,
            TimeoutError,
            OSError,
        ) as e:
            exception_to_structured_error(
                e,
                context={"source": "system"},
                suggestions=[
                    "Check Home Assistant WebSocket connection",
                    "Verify system_log integration is enabled",
                ],
            )
            raise  # unreachable: exception_to_structured_error always raises

    def _build_structured_error_log(
        self,
        raw_log: str,
        search: str | None,
        level: str | None,
        top_n: int | None,
        limit: int | None,
        order: Literal["newest", "oldest"],
    ) -> dict[str, Any]:
        """Summarise the raw error log and annotate what shaped the result."""
        effective_top_n = _coerce_limit(
            top_n,
            default=_DEFAULT_TOP_N,
            suggestion_example="20",
            param_name="top_n",
        )
        result = _parse_error_log_structured(
            raw_log,
            search=search,
            level=level,
            top_n=effective_top_n,
        )
        # Report the filters that shaped the summary, matching the raw path —
        # otherwise an empty summary is indistinguishable from a quiet log.
        structured_filters = {
            k: v for k, v in (("level", level), ("search", search)) if v
        }
        if structured_filters:
            result["filters_applied"] = structured_filters
        # `limit`/`order` are accepted for signature compatibility but do nothing
        # here; say so rather than silently ignoring them, which is the
        # convention this tool already uses elsewhere.
        ignored = [
            name
            for name, given in (
                ("limit", limit is not None),
                ("order", order != "newest"),
            )
            if given
        ]
        if ignored:
            # Append: the parser may already have warned about format drift, and
            # overwriting that would drop the "this is NOT an all-clear" notice.
            result.setdefault("warnings", []).append(
                f"Parameter(s) {', '.join(ignored)} do not apply when "
                "structured=True; the summary ranks the whole fetched window "
                "by occurrence count. Use top_n to bound the output."
            )
        return result

    @staticmethod
    def _filter_error_log_lines(
        lines: list[str], search: str | None, level: str | None
    ) -> tuple[list[tuple[int, str]], dict[str, str]]:
        """Filter window lines, keeping each survivor's index in the window.

        The index is what makes paging exact: the next page has to resume where
        the returned block starts, not a whole window further back, or a filter
        that drops most of a window silently skips everything it dropped.
        """
        indexed = list(enumerate(lines))
        filters_applied: dict[str, str] = {}

        if level:

            def _line_has_level(ln: str, target: str) -> bool:
                m = _LOG_LEVEL_RE.search(ln)
                return m is not None and m.group(1).upper() == target

            indexed = [(i, ln) for i, ln in indexed if _line_has_level(ln, level)]
            filters_applied["level"] = level

        if search:
            search_lower = search.lower()
            indexed = [(i, ln) for i, ln in indexed if search_lower in ln.lower()]
            filters_applied["search"] = search

        return indexed, filters_applied

    def _build_raw_error_log(
        self,
        raw_log: str,
        search: str | None,
        level: str | None,
        effective_limit: int,
        order: Literal["newest", "oldest"],
        window: _ErrorLogWindow,
    ) -> tuple[dict[str, Any], int]:
        """Return the most recent matching lines, plus the next page's step.

        The step is how far back ``offset`` must move to resume just before the
        oldest line returned here. ``effective_limit`` arrives already coerced:
        the caller needs the same value for the pagination hint, and the
        summary path must not coerce it at all.
        """
        matches, filters_applied = self._filter_error_log_lines(
            raw_log.splitlines(), search, level
        )

        total_lines = len(matches)
        # Always take the most-recent window (the tail of the chronological
        # file); 'order' controls only the display direction of that window.
        shown = matches[-effective_limit:]
        step = _next_page_step(window, shown)
        lines = [ln for _, ln in shown]
        if order == "newest":
            lines = list(reversed(lines))

        data: dict[str, Any] = {
            "success": True,
            "source": "error_log",
            "log": "\n".join(lines),
            "total_lines": total_lines,
            "returned_lines": len(lines),
            "limit": effective_limit,
            "window_lines": window.fetch_lines,
            "order": order,
            "note": (
                "Returned the most recent log lines matching filters, from a "
                "bounded window of the log. 'offset' counts raw log lines back "
                "from the newest entry; filters apply within each fetched "
                "window, so 'total_lines' counts matches in this window only"
            ),
        }
        if filters_applied:
            data["filters_applied"] = filters_applied
        return data, step

    @staticmethod
    def _apply_empty_window_warning(
        data: dict[str, Any], raw_text: str, offset: int, structured: bool
    ) -> None:
        """Tell an empty terminal page apart from an empty fetch.

        At ``offset=0`` an empty response is the alarming case: a running
        instance always logs something, so nothing at the newest edge means the
        fetch produced nothing — without saying so, an agent reports "no errors
        in the log". Deeper into history an empty window is just the end of the
        record, and ``_EMPTY_FETCH_WARNING`` would send the caller after an
        outage that is not happening; the structured parser emits it
        unconditionally, so it is replaced there rather than contradicted.
        """
        if raw_text.strip():
            return
        if offset:
            kept = [
                w for w in data.get("warnings", []) if _EMPTY_FETCH_WARNING not in w
            ]
            data["warnings"] = kept + [
                f"No log content at offset={offset}: the recorded history ends "
                "before this window. Lower 'offset' to reach the entries that exist."
            ]
        elif not structured:
            # The structured path's parser emits its own version of this.
            data.setdefault("warnings", []).append(
                f"The fetch returned no log content. {_EMPTY_FETCH_WARNING}"
            )

    async def _get_error_log(
        self,
        limit: int | None = None,
        search: str | None = None,
        level: str | None = None,
        order: Literal["newest", "oldest"] = "newest",
        offset: int = 0,
        structured: bool = False,
        top_n: int | None = None,
    ) -> dict[str, Any]:
        """Fetch a bounded window of error log text (home-assistant.log, or journald).

        Container/pip installs read the plain ``home-assistant.log`` file;
        Supervisor-backed installs read HA Core's journald stream instead.
        Either way only the requested window is fetched, and ``offset`` pages
        deeper into the history (#2279) — in file lines on container installs,
        in journald entries on Supervisor-backed ones.

        With ``structured=True`` the raw text is collapsed into a counted,
        component-grouped summary instead (see ``_parse_error_log_structured``);
        ``limit``/``order`` do not apply in that mode (the summary ranks the
        whole fetched window by occurrence count rather than returning a
        positional slice of it), and ``top_n`` bounds it instead.
        """
        try:
            fetch_lines = _error_log_window_lines(limit, search, level, structured)
            page = await self._client.get_error_log(lines=fetch_lines, offset=offset)
            raw_text = page.text or ""
            window = _ErrorLogWindow(
                offset=offset,
                fetch_lines=fetch_lines,
                raw_line_count=len(raw_text.splitlines()),
                has_more=page.has_more,
            )

            if structured:
                data = self._build_structured_error_log(
                    raw_text,
                    search=search,
                    level=level,
                    top_n=top_n,
                    limit=limit,
                    order=order,
                )
                data["window_lines"] = fetch_lines
                # The summary consumes the whole window, so the next page
                # starts a whole window further back.
                step = fetch_lines
                effective_limit = None
            else:
                effective_limit = _coerce_limit(
                    limit, default=DEFAULT_LOG_LIMIT, suggestion_example="100"
                )
                data, step = self._build_raw_error_log(
                    raw_text,
                    search=search,
                    level=level,
                    effective_limit=effective_limit,
                    order=order,
                    window=window,
                )
            self._apply_empty_window_warning(data, raw_text, offset, structured)
            _attach_error_log_pagination(
                data,
                window,
                step,
                effective_limit,
                search,
                level,
                order,
                structured,
                top_n,
                # Raw path only: the structured summary consumes the whole
                # window, so it never leaves matches behind a limit slice.
                unreturned_matches=(
                    0 if structured else data["total_lines"] - data["returned_lines"]
                ),
            )
            return data

        except ToolError:
            raise
        except HomeAssistantAuthError as e:
            # AuthError is a sibling of HomeAssistantAPIError, not a subclass,
            # so the tuple below never catches it and a 401 would propagate raw
            # to FastMCP without a structured `code`. All three fetch branches
            # (addon Supervisor, hassio proxy, /api/error_log) can raise it.
            exception_to_structured_error(
                e,
                context={"source": "error_log"},
                suggestions=_addon_auth_error_suggestions(),
            )
            raise  # unreachable: exception_to_structured_error always raises
        except (
            HomeAssistantConnectionError,
            HomeAssistantAPIError,
            TimeoutError,
            OSError,
        ) as e:
            exception_to_structured_error(
                e,
                context={"source": "error_log"},
                suggestions=[
                    "Check Home Assistant connection",
                    "The error log may be empty if no errors have occurred",
                ],
            )
            raise  # unreachable: exception_to_structured_error always raises

    @staticmethod
    def _parse_logger_entry(entry: Any) -> dict[str, Any] | None:
        if not isinstance(entry, dict):
            return None
        domain = entry.get("domain")
        if not isinstance(domain, str) or not domain:
            return None
        raw_level = entry.get("level")
        level_name = normalize_log_level(raw_level)
        if level_name is None:
            return None
        return {
            "domain": domain,
            "level": level_name,
            "level_raw": raw_level if isinstance(raw_level, int) else None,
        }

    async def _get_logger_info(
        self,
        limit: int | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        """Fetch per-integration log levels via the ``logger/log_info`` WS command."""
        effective_limit = _coerce_limit(limit)

        try:
            result = await self._client.send_websocket_message(
                {"type": "logger/log_info"}
            )

            if not result.get("success"):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        result.get("error", "Failed to retrieve logger info"),
                        suggestions=[
                            "Verify the 'logger' integration is enabled in Home Assistant",
                            "Check Home Assistant WebSocket connection",
                        ],
                    )
                )

            raw_entries = result.get("result", [])
            if not isinstance(raw_entries, list):
                raw_entries = []

            loggers: list[dict[str, Any]] = []
            for entry in raw_entries:
                parsed = self._parse_logger_entry(entry)
                if parsed is not None:
                    loggers.append(parsed)

            filters_applied: dict[str, str] = {}
            if search:
                search_lower = search.lower()
                loggers = [
                    entry
                    for entry in loggers
                    if search_lower in entry["domain"].lower()
                ]
                filters_applied["search"] = search

            loggers.sort(key=lambda entry: entry["domain"])

            total_entries = len(loggers)
            loggers = loggers[:effective_limit]

            data: dict[str, Any] = {
                "success": True,
                "source": "logger",
                "loggers": loggers,
                "total_entries": total_entries,
                "returned_entries": len(loggers),
                "limit": effective_limit,
            }
            if filters_applied:
                data["filters_applied"] = filters_applied

            return data

        except ToolError:
            raise
        except (
            HomeAssistantConnectionError,
            HomeAssistantAPIError,
            TimeoutError,
            OSError,
        ) as e:
            exception_to_structured_error(
                e,
                context={"source": "logger"},
                suggestions=[
                    "Check Home Assistant WebSocket connection",
                    "Verify the 'logger' integration is enabled",
                ],
            )
            raise  # unreachable: exception_to_structured_error always raises
