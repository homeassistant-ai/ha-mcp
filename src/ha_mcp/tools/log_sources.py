"""Log sources served by ``ha_get_logs`` from Home Assistant itself.

``system_log``, the raw/structured error log, and the per-integration logger
levels. The logbook lives in ``log_sources_logbook``; the Supervisor-backed
sources are in ``log_sources_supervisor``. Mixed into ``LogTools``
(``tools_logs``) rather than standing alone: every fetcher here is a thin shell
around one client call, and the dispatch, validation and tool registration they
share live with the host class.

Split out of ``tools_utility`` under `.gemini/styleguide.md` § Tool
Consolidation and Module Size.
"""

import logging
from typing import Any, Literal

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..client.rest_client import (
    ErrorLogPage,
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from .error_log_parsing import (
    _DEFAULT_TOP_N,
    _EMPTY_FETCH_WARNING,
    _ERROR_LOG_HEADER_LOOKBACK_LINES,
    _attach_error_log_pagination,
    _error_log_window_lines,
    _ErrorLogWindow,
    _filter_log_records,
    _LogRecord,
    _next_page_step,
    _parse_error_log_structured,
    _RecordPage,
    _reopen_at_record_end,
    _select_error_log_page,
    _split_log_records,
    _strip_ansi,
    _TruncatedRecord,
)
from .helpers import exception_to_structured_error, raise_tool_error
from .log_common import (
    DEFAULT_LOG_LIMIT,
    MAX_LIMIT,
    SUPERVISOR_SEARCH_WINDOW_LINES,
    _addon_auth_error_suggestions,
    _coerce_limit,
)
from .util_helpers import normalize_log_level

logger = logging.getLogger(__name__)


def _leading_tail_policy(
    *,
    window: _ErrorLogWindow,
    leading_record: _LogRecord | None,
    page: _RecordPage,
    matches: list[_LogRecord],
    search: str | None,
    refetch_failed: bool,
) -> tuple[bool, bool]:
    """Decide what to do with a window that opens inside a record.

    Returns ``(held_back, header_out_of_reach)``.

    Holding the tail back only pays off when the next window can reach its
    header, so a record longer than a whole fetch window is returned as a
    partial instead. The tail is only a candidate when the filters could
    select it: ``search`` has to hit one of its lines, while a ``level`` read
    cannot tell because the level lives in the missing header.
    """
    if leading_record is None or not window.has_more or refetch_failed:
        return False, False
    search_hits_tail = search is None or any(
        search.lower() in _strip_ansi(line).lower() for line in leading_record.lines
    )
    header_reachable = leading_record.line_count < window.fetch_lines
    if not header_reachable:
        return False, page.leading_partial
    held_back = search_hits_tail and len(page.records) == len(matches)
    return held_back, False


def _raw_error_log_warnings(
    *,
    held_back: bool,
    header_out_of_reach: bool,
    level: str | None,
    refetch_failed: bool,
    leading_partial: bool,
    truncated: _TruncatedRecord | None,
    effective_limit: int,
) -> list[str]:
    """Warnings that explain what one raw error-log page did and did not include."""
    warnings: list[str] = []
    if held_back:
        subject = (
            "The oldest record at this window's edge"
            if level
            else "The oldest matching record"
        )
        warnings.append(
            f"{subject} opens before this window; it is "
            "held back so the next page can return it whole with its header."
        )
    if refetch_failed:
        warnings.append(
            "The wider read that would have brought the oldest record's "
            "header failed; this page is the first window only."
        )
    if leading_partial:
        if header_out_of_reach:
            gate = "the record is longer than one fetch window"
        elif refetch_failed:
            gate = "a wider read failed before reaching it"
        else:
            gate = "it lies outside the fetched history"
        subject = (
            "The oldest lines at this window's edge are"
            if level
            else "The oldest matched lines are"
        )
        warnings.append(
            f"{subject} the tail of a record whose "
            f"header could not be included because {gate}; they are "
            "returned as a partial record."
        )
    if truncated is not None:
        cut = truncated.total_lines - truncated.returned_lines
        kept = (
            "only its header"
            if truncated.returned_lines <= 1
            else f"its header and last {truncated.returned_lines - 1} lines"
        )
        if truncated.total_lines <= MAX_LIMIT:
            warnings.append(
                f"The newest record is {truncated.total_lines} lines; "
                f"'limit'={effective_limit} returned {kept} and cut {cut} "
                f"from the middle. Repeat with "
                f"limit={truncated.total_lines} to return it whole."
            )
        else:
            warnings.append(
                f"The newest record is {truncated.total_lines} lines, "
                f"longer than the per-call maximum ({MAX_LIMIT}); {cut} "
                "lines were cut."
            )
    return warnings


# system_log keeps a message verbatim, so one warning that reprs a binary
# payload (a blocking-call report on write_bytes, say) can run to hundreds of
# kilobytes. The cap is per string, and an entry keeps up to five messages, so
# one entry can still return about five times the cap.
_SYSTEM_LOG_MESSAGE_CAP = 2000


def _truncation_marker(cut: int) -> str:
    return (
        f"…[{cut:,} chars cut; compact=False with a narrow search and "
        f"limit=1 returns it whole]…"
    )


def _cap_message(message: str) -> tuple[str, bool]:
    """Keep both ends of an oversized message, the marker in between.

    The lead names what happened and the end carries the innermost stack
    frames that identify the offender; the middle is usually the payload.
    """
    if len(message) <= _SYSTEM_LOG_MESSAGE_CAP:
        return message, False
    cut = len(message)
    # The marker's width depends on the digits of the cut size, so settle it.
    while True:
        budget = _SYSTEM_LOG_MESSAGE_CAP - len(_truncation_marker(cut))
        settled = len(message) - budget
        if settled == cut:
            break
        cut = settled
    head = budget // 2
    tail = budget - head
    return message[:head] + _truncation_marker(cut) + message[-tail:], True


def _cap_system_log_messages(entries: list[Any]) -> int:
    """Cap oversized system_log message strings in place; return how many."""
    capped = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        message = entry.get("message")
        if isinstance(message, str):
            entry["message"], cut = _cap_message(message)
            capped += cut
        elif isinstance(message, list):
            parts = []
            for part in message:
                if isinstance(part, str):
                    kept, cut = _cap_message(part)
                    capped += cut
                    parts.append(kept)
                else:
                    parts.append(part)
            entry["message"] = parts
    return capped


class CoreLogSourcesMixin:
    """The log sources HA Core serves directly.

    ``_client`` is supplied by the host class (``LogTools``); this mixin adds
    no state of its own.
    """

    _client: Any

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
        compact: bool = True,
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
                        or search_lower in str(e.get("exception", "")).lower()
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
            truncated_messages = _cap_system_log_messages(entries) if compact else 0

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
            if truncated_messages:
                data["truncated_messages"] = truncated_messages

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

    async def _widen_error_log_for_record_header(
        self,
        fetch_lines: int,
        offset: int | str,
        raw_text: str,
        page: ErrorLogPage,
    ) -> tuple[int, ErrorLogPage, str, bool]:
        """Re-fetch once when the fetched window opens inside a record.

        ``offset`` addresses the newest edge, so increasing ``lines`` extends
        the same window further back. The extra room brings a traceback's
        header into a page that would otherwise start mid-record. Capped at the
        search-window bound used elsewhere, so this never becomes an unbounded
        read.

        Skipped when the page already reaches the start of history: there is
        nothing older to fetch, and on container installs each call downloads
        the whole log file.

        Returns the fourth element ``refetch_failed`` so the caller can say
        that a header was left out by a failed read rather than because the
        history ends.
        """
        if not page.has_more:
            return fetch_lines, page, raw_text, False
        records = _split_log_records(raw_text.splitlines())
        if not records or records[0].header is not None:
            return fetch_lines, page, raw_text, False
        leading = records[0].line_count
        wider = min(
            fetch_lines + leading + _ERROR_LOG_HEADER_LOOKBACK_LINES,
            SUPERVISOR_SEARCH_WINDOW_LINES,
        )
        if wider <= fetch_lines:
            return fetch_lines, page, raw_text, False
        try:
            wider_page = await self._client.get_error_log(lines=wider, offset=offset)
        except (
            HomeAssistantConnectionError,
            HomeAssistantAPIError,
            HomeAssistantAuthError,
            TimeoutError,
            OSError,
        ) as e:
            # The first page is already usable; losing the header is better
            # than losing the read. Say so rather than discarding the reason.
            logger.warning(
                "error_log: wider read for a record header failed (%s); "
                "continuing with the first window",
                e,
            )
            return fetch_lines, page, raw_text, True
        return wider, wider_page, wider_page.text or "", False

    def _build_raw_error_log(
        self,
        raw_log: str,
        search: str | None,
        level: str | None,
        effective_limit: int,
        order: Literal["newest", "oldest"],
        window: _ErrorLogWindow,
        refetch_failed: bool = False,
    ) -> tuple[dict[str, Any], int, int, bool]:
        """Return the most recent matching records, plus both next-page anchors.

        Records are the unit: ``search`` matches any line of a record and
        ``level`` matches its header, then whole records are selected within
        the caller's line budget. ``order`` controls display direction only
        (the selected window is always the most recent one); a record's own
        lines never reverse.

        Two anchors come back, because the two install classes page
        differently. The integer step resumes just before the oldest returned
        record counted from the newest line, which is what ``offset`` means on
        container installs. The cursor skip counts entries forward from the
        fetched window's first cursor, which is what a journald token means:
        nothing fetched behind the page is lost and nothing returned is
        repeated, since the next block ends exactly one entry behind the
        resume point. Core's journald stream carries one entry per output line
        (even a split long line arrives as one line per entry), so a line index
        in the fetched window is the entry index the skip counts.

        A filtered window can open inside a record, and there the same fixed
        width keeps it opening inside that record every page. When history
        remains and nothing behind the page went unreturned, the tail is held
        back and both anchors re-open the next page at the record's last line,
        so the record arrives whole with its header instead of split in two.
        """
        records = _split_log_records(raw_log.splitlines())
        leading_record = records[0] if records and records[0].header is None else None
        matches, filters_applied = _filter_log_records(records, search, level)
        page = _select_error_log_page(matches, effective_limit, order)
        step = _next_page_step(window, page.oldest_index)

        held_back, header_out_of_reach = _leading_tail_policy(
            window=window,
            leading_record=leading_record,
            page=page,
            matches=matches,
            search=search,
            refetch_failed=refetch_failed,
        )
        if held_back and leading_record is not None:
            # Filter the leading tail out BEFORE selecting, so a record the
            # page had to cut cannot survive in a copy: the response would
            # then report a returned record while its counts exclude it.
            matches = [record for record in matches if record is not leading_record]
            page = _select_error_log_page(matches, effective_limit, order)
            step = _reopen_at_record_end(window, leading_record)
            cursor_skip = leading_record.start_index + leading_record.line_count
        else:
            # Zero when nothing was shown: the whole fetched window is consumed.
            cursor_skip = page.oldest_index or 0

        total_lines = sum(record.line_count for record in matches)
        # Matches older than the oldest returned record are reachable by paging
        # again from a token; the ones this page cut out of a single over-long
        # record are not, they need a larger limit.
        older_matches_remain = (
            any(record.start_index < page.oldest_index for record in matches)
            if page.oldest_index is not None
            else bool(matches)
        )

        data: dict[str, Any] = {
            "success": True,
            "source": "error_log",
            "log": "\n".join(page.lines),
            "total_lines": total_lines,
            "returned_lines": len(page.lines),
            "total_records": len(matches),
            "returned_records": len(page.records),
            "limit": effective_limit,
            "window_lines": window.fetch_lines,
            "order": order,
            "note": (
                "Returned the most recent log records matching filters, from a "
                "bounded window of the log. 'offset' is either an integer count "
                "back from the newest entry (raw lines on container/pip "
                "installs, journald entries on Supervisor-backed ones) or the "
                "opaque 'next_offset' token a previous page returned; filters "
                "apply within each fetched window, so 'total_lines' counts the "
                "lines of matching records in this window only. 'order' "
                "controls display direction; a record's lines always stay in "
                "their original order."
            ),
        }
        warnings = _raw_error_log_warnings(
            held_back=held_back,
            header_out_of_reach=header_out_of_reach,
            level=level,
            refetch_failed=refetch_failed,
            leading_partial=page.leading_partial,
            truncated=page.truncated,
            effective_limit=effective_limit,
        )
        if warnings:
            data["warnings"] = warnings
        if filters_applied:
            data["filters_applied"] = filters_applied
        return data, step, cursor_skip, older_matches_remain

    @staticmethod
    def _apply_empty_window_warning(
        data: dict[str, Any], raw_text: str, offset: int | str, structured: bool
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
        offset: int | str = 0,
        structured: bool = False,
        top_n: int | None = None,
    ) -> dict[str, Any]:
        """Fetch a bounded window of error log text (home-assistant.log, or journald).

        Container/pip installs read the plain ``home-assistant.log`` file;
        Supervisor-backed installs read HA Core's journald stream instead.
        Either way only the requested window is fetched, and ``offset`` pages
        deeper into the history (#2279): in file lines on container installs,
        and either an integer entry offset or the opaque cursor token a
        previous journald page returned on Supervisor-backed ones.

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
            refetch_failed = False
            if not structured:
                (
                    fetch_lines,
                    page,
                    raw_text,
                    refetch_failed,
                ) = await self._widen_error_log_for_record_header(
                    fetch_lines, offset, raw_text, page
                )
            window = _ErrorLogWindow(
                offset=offset,
                fetch_lines=fetch_lines,
                raw_line_count=len(raw_text.splitlines()),
                has_more=page.has_more,
                first_cursor=page.first_cursor,
            )

            data: dict[str, Any]
            # The structured summary consumes its whole window, so its token
            # resumes strictly behind the window's oldest entry; the raw path
            # sets the skip from the records it returned. Container installs
            # have no cursor, so both fall back to the integer line step.
            cursor_skip: int | None = None
            older_matches_remain = False
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
                cursor_skip = 0
            else:
                effective_limit = _coerce_limit(
                    limit, default=DEFAULT_LOG_LIMIT, suggestion_example="100"
                )
                data, step, cursor_skip, older_matches_remain = (
                    self._build_raw_error_log(
                        raw_text,
                        search=search,
                        level=level,
                        effective_limit=effective_limit,
                        order=order,
                        window=window,
                        refetch_failed=refetch_failed,
                    )
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
                cursor_skip=cursor_skip,
                older_matches_remain=(False if structured else older_matches_remain),
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
