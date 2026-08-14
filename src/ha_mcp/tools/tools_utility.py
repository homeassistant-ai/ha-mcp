"""
Utility tools for Home Assistant MCP server.

This module provides general-purpose utility tools including log access,
template evaluation, and domain documentation retrieval.
"""

import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, NamedTuple, NoReturn

from fastmcp.exceptions import ToolError
from pydantic import Field

from .._version import is_running_in_addon
from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, log_tool_usage, raise_tool_error
from .util_helpers import (
    add_timezone_metadata,
    normalize_log_level,
)

logger = logging.getLogger(__name__)

# Fields to keep in compact logbook mode (strips attribute dictionaries
# and other bulky fields that can cause context exhaustion — see #683)
COMPACT_LOGBOOK_FIELDS = {
    "when",
    "entity_id",
    "state",
    "name",
    "message",
    "domain",
    "context_id",
    "source",
}


# Supervisor-managed system services exposed via /<slug>/logs. Set mirrors
# HA Core's hassio HTTP proxy ``PATHS_ADMIN`` whitelist in
# ``homeassistant/components/hassio/http.py``. See #1116 (original 7-service
# scope) and #1260 (cli added — proxy supported it the whole time).
SYSTEM_SERVICE_SLUGS = frozenset(
    {"supervisor", "host", "core", "dns", "audio", "cli", "multicast", "observer"}
)

DEFAULT_LIMIT = 50
DEFAULT_LOG_LIMIT = 100
# Journald window to request from Supervisor when a search filter is
# active: matches are found within the fetched window only, so a
# search over the caller's (often small) limit needs more history
# behind it than the limit itself.
SUPERVISOR_SEARCH_WINDOW_LINES = 2000
MAX_LIMIT = 500

# Regex to match log level at the start of a log line
_LOG_LEVEL_RE = re.compile(
    r"(?:^|\s)(DEBUG|INFO|WARNING|ERROR|CRITICAL)(?:\s|:|\])", re.IGNORECASE
)

VALID_LOG_LEVELS = ("ERROR", "WARNING", "INFO", "DEBUG", "CRITICAL")

# Full HA log line, e.g.
# "2026-05-27 10:15:23.456 ERROR (MainThread) [homeassistant.components.zha] msg"
# Distinct from _LOG_LEVEL_RE above, which only sniffs the level out of a line.
# The thread field is matched lazily instead of as `\([^)]*\)`: since Python 3.10
# an unnamed thread is called "Thread-1 (target_fn)", so the field itself can
# contain parentheses, and a class that cannot cross them drops every line those
# threads log.
_HA_LOG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?)"
    r"\s+(DEBUG|INFO|WARNING|ERROR|CRITICAL)"
    r"\s+\(.*?\)"
    r"\s+\[([^\]]+)\]"
    r"\s+(.+)$",
    re.IGNORECASE,
)
_MAX_MESSAGE_LEN = 200
_COMPONENT_PREFIX_DEPTH = 3
_CUSTOM_COMPONENT_PREFIX_DEPTH = 2
_DEFAULT_TOP_N = 20
_MAX_COMPONENTS = 50
# Marks a message the 200-char cap cut short, so a truncated entry is never
# mistaken for the whole message.
_TRUNCATION_MARK = "…[truncated]"
# Tie-breaker for issues with equal counts: a same-count CRITICAL outranks an
# INFO. Ordering only — not a filter, and not tied to VALID_LOG_LEVELS.
_LEVEL_SEVERITY = {"CRITICAL": 4, "ERROR": 3, "WARNING": 2, "INFO": 1, "DEBUG": 0}
# Shared by both response paths, because an empty fetch reads as an all-clear on
# either one: an agent that receives no log content and no warning reports "no
# errors" to the user. Only the remedy differs, so each path appends its own.
_EMPTY_FETCH_WARNING = (
    "A running Home Assistant always logs something, so this is an empty or "
    "failed fetch, NOT evidence that the log is clean."
)

# Supervisor-backed installs (add-on, HAOS, supervised) read HA Core's journald
# stream, where every line is wrapped in ANSI colour codes:
#   "\x1b[31m2026-08-02 08:12:27.290 ERROR (MainThread) [x] msg\x1b[0m"
# Only container/pip installs read the plain home-assistant.log file.
# Two independent reasons to strip, not one chain:
#   * the leading code alone is already fatal — _HA_LOG_LINE_RE is ^-anchored
#     and str.strip() does not remove ESC, so the line matches nothing and no
#     `message` is produced at all;
#   * the trailing reset is what a leading-only strip would leave behind: it
#     lands inside `message` and splits the dedup key for one recurring error.
# Strip both ends, always.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences (journald colour codes) from a log line."""
    return _ANSI_RE.sub("", text)


def _normalize_timestamp(timestamp: str) -> str:
    """Put a matched timestamp on one date/time separator.

    Every timestamp comparison downstream is lexicographic — the covered
    window, an issue's ``first_seen``/``last_seen`` bounds, and the recency
    tiebreaker. ``_HA_LOG_LINE_RE`` accepts both the space and the ``T`` form
    (case-insensitively), and ``" "`` sorts below every digit while ``"T"``
    sorts above them, so a stream mixing the two forms would corrupt all three
    at once. HA core emits the space form, so normalize onto that: real logs
    come out byte-identical and only the ISO-8601 variant is rewritten.

    The date is fixed-width in the pattern (``\\d{4}-\\d{2}-\\d{2}``), so the
    separator is always at index 10 — replacing by position also covers the
    lowercase ``t`` that ``re.IGNORECASE`` admits.
    """
    return timestamp[:10] + " " + timestamp[11:]


def _get_component_prefix(logger_name: str) -> str:
    """Extract the component prefix (first N dotted segments) of a logger name.

    A custom integration's identity is ``custom_components.<domain>`` — one
    segment shallower than a core component — so its modules roll up into one
    bucket instead of one per module.
    """
    parts = logger_name.split(".")
    depth = (
        _CUSTOM_COMPONENT_PREFIX_DEPTH
        if parts[0] == "custom_components"
        else _COMPONENT_PREFIX_DEPTH
    )
    if len(parts) >= depth:
        return ".".join(parts[:depth])
    return logger_name


def _truncate_message(message: str) -> str:
    """Cap a message for display, marking it when it was cut."""
    if len(message) <= _MAX_MESSAGE_LEN:
        return message
    return message[:_MAX_MESSAGE_LEN] + _TRUNCATION_MARK


class _ExtractedLines(NamedTuple):
    """Tallies from one pass over the raw log; every input line lands in one.

    ``matched`` counts lines that are valid HA log lines *before* ``level`` and
    ``search`` run; ``entries`` holds the subset that survived them. The two are
    kept apart because they answer different questions: only ``matched == 0``
    means the parser could not read the log, while ``matched > 0`` with no
    entries just means the filters excluded everything.

    ``total input lines == blank + unparseable + matched`` holds by construction.
    """

    entries: list[dict[str, str]]
    matched: int
    unparseable: int
    blank: int
    window_start: str | None
    window_end: str | None


def _extract_log_entries(
    lines: list[str],
    search: str | None = None,
    level: str | None = None,
) -> _ExtractedLines:
    """Parse raw log lines into entries plus the per-line tallies around them.

    Split out from ``_parse_error_log_structured`` to keep both under the repo's
    complexity ceiling. A line counts as unparseable only when it does not match
    the HA log format at all — lines dropped by ``level``/``search`` are simply
    absent from ``entries``, never counted as unparseable.
    """
    entries: list[dict[str, str]] = []
    matched = 0
    unparseable = 0
    blank = 0
    window_start: str | None = None
    window_end: str | None = None
    needle = search.lower() if search else None

    for line in lines:
        # Strip ANSI *before* matching: on Supervisor-backed installs the line
        # arrives colour-wrapped. Both ends matter for separate reasons — the
        # leading code alone already defeats the ^-anchor so the line matches
        # nothing, and a leading-only strip would leave the trailing reset
        # inside `message`, splitting the dedup key for one recurring error.
        clean = _strip_ansi(line).strip()
        if not clean:
            blank += 1
            continue
        match = _HA_LOG_LINE_RE.match(clean)
        if not match:
            unparseable += 1
            continue
        matched += 1
        timestamp, log_level, logger_name, message = match.groups()
        timestamp = _normalize_timestamp(timestamp)
        # Widen the covered window on every parseable line, filtered or not:
        # it describes the log slice that was read, not the summary's contents.
        if window_start is None or timestamp < window_start:
            window_start = timestamp
        if window_end is None or timestamp > window_end:
            window_end = timestamp
        log_level = log_level.upper()
        if level and log_level != level:
            continue
        if (
            needle
            and needle not in message.lower()
            and needle not in logger_name.lower()
        ):
            continue
        entries.append(
            {
                "timestamp": timestamp,
                "level": log_level,
                "logger": logger_name,
                # Kept whole: the display cap is applied at dedup time, because
                # keying on a capped message merges two errors that differ only
                # past the cap into one issue with a summed count.
                "message": message,
            }
        )

    return _ExtractedLines(
        entries=entries,
        matched=matched,
        unparseable=unparseable,
        blank=blank,
        window_start=window_start,
        window_end=window_end,
    )


def _parse_error_log_structured(
    raw_text: str,
    search: str | None = None,
    level: str | None = None,
    top_n: int = _DEFAULT_TOP_N,
) -> dict[str, Any]:
    """Parse the raw error log into a deduplicated, component-grouped summary.

    ``source='error_log'`` normally returns raw text. A busy instance produces a
    50-200 KB log in which the same handful of errors repeat thousands of times,
    so the raw form can exhaust an agent's context while conveying very little.
    This collapses identical (level, logger, message) triples into counted
    issues, groups them by component, and returns only the ``top_n`` noisiest.

    Every input line is accounted for exactly once, because "not in the summary"
    has several causes that must not be conflated:

    * ``total_raw_lines``   - lines in the input, and the sum of the next three
    * ``blank_lines``       - empty after ANSI stripping
    * ``unparseable_lines`` - not the HA log format at all (traceback bodies,
      continuation lines, format drift)
    * ``matched_lines``     - valid log lines, before ``level``/``search``
    * ``parsed_entries``    - the subset of ``matched_lines`` the filters kept

    Only ``matched_lines == 0`` says the parser could not read the log; a small
    ``parsed_entries`` under a narrow filter is the feature working, not data
    loss. Counts are bounded by the fetched window (``window_start`` ..
    ``window_end``): Supervisor-backed installs read a capped journald slice, so
    there the window can be far shorter than the log's full history.
    """
    lines = raw_text.splitlines() if raw_text else []
    total_raw_lines = len(lines)
    extracted = _extract_log_entries(lines, search=search, level=level)
    parsed = extracted.entries

    # Dedupe on (level, logger, message), keyed on the FULL message: two errors
    # that differ only past the display cap are distinct issues, and a capped
    # key merges them into one with a summed count. Level belongs in the key
    # too: the same text is logged at different levels, and a level-less key
    # would report a later ERROR under the level of the first line to carry it.
    dedup: dict[tuple[str, str, str], dict[str, Any]] = {}
    for entry in parsed:
        key = (entry["level"], entry["logger"], entry["message"])
        record = dedup.get(key)
        if record is None:
            record = {
                "logger": entry["logger"],
                "level": entry["level"],
                "message": _truncate_message(entry["message"]),
                "count": 0,
                "first_seen": entry["timestamp"],
                "last_seen": entry["timestamp"],
            }
            dedup[key] = record
        record["count"] += 1
        # Bounds, not last-write-wins: a log whose lines are not perfectly
        # ordered would otherwise drag last_seen backwards, which also
        # misplaces the issue in the recency tiebreaker below.
        record["first_seen"] = min(record["first_seen"], entry["timestamp"])
        record["last_seen"] = max(record["last_seen"], entry["timestamp"])

    # Count first, then severity, then recency. Python's sort is stable, so
    # count alone would return the *oldest* of a tied run — and on a log whose
    # messages embed volatile ids every issue ties at count 1, which quietly
    # turns "top 20 issues" into "the 20 oldest".
    all_issues = sorted(
        dedup.values(),
        key=lambda i: (
            i["count"],
            _LEVEL_SEVERITY.get(i["level"], 0),
            i["last_seen"],
        ),
        reverse=True,
    )

    by_component: dict[str, dict[str, Any]] = {}
    for issue in all_issues:
        prefix = _get_component_prefix(issue["logger"])
        bucket = by_component.setdefault(
            prefix, {"total_occurrences": 0, "issue_count": 0}
        )
        bucket["total_occurrences"] += issue["count"]
        bucket["issue_count"] += 1

    top_issues = [
        {
            "component": _get_component_prefix(issue["logger"]),
            "logger": issue["logger"],
            "level": issue["level"],
            "message": issue["message"],
            "count": issue["count"],
            "first_seen": issue["first_seen"],
            "last_seen": issue["last_seen"],
        }
        for issue in all_issues[:top_n]
    ]

    # Cap by_component too: a log with thousands of distinct loggers would
    # otherwise make the response unbounded, contradicting the whole premise.
    ranked_components = sorted(
        by_component.items(),
        key=lambda kv: kv[1]["total_occurrences"],
        reverse=True,
    )
    component_table = dict(ranked_components[:_MAX_COMPONENTS])

    summary: dict[str, Any] = {
        "total_raw_lines": total_raw_lines,
        "blank_lines": extracted.blank,
        "unparseable_lines": extracted.unparseable,
        "matched_lines": extracted.matched,
        "parsed_entries": len(parsed),
        "unique_issues": len(all_issues),
        "components_affected": len(by_component),
        "showing_top_n": min(top_n, len(all_issues)),
        "showing_components": len(component_table),
        # Counts describe this window only. On Supervisor-backed installs the
        # fetch is a capped journald slice, so an issue's count here is not its
        # count since it first occurred.
        "window_start": extracted.window_start,
        "window_end": extracted.window_end,
    }

    result: dict[str, Any] = {
        "success": True,
        "source": "error_log",
        "structured": True,
        "summary": summary,
        "top_issues": top_issues,
        "by_component": component_table,
    }

    # An empty summary has three causes and they need different answers, so the
    # branch keys on `matched_lines` — the pre-filter count. Keying on the raw
    # unparseable count instead reports format drift for an ordinary filtered
    # result, because tracebacks alone push a healthy log past 90% unparseable.
    # Degraded-operation notices go in the top-level `warnings` list — the one
    # channel this repo's tools use — never a singular `warning` string.
    if extracted.matched == 0:
        if extracted.unparseable > 0:
            result["warnings"] = [
                f"No log lines could be parsed ({extracted.unparseable} of "
                f"{total_raw_lines} did not match the expected Home Assistant log "
                "format). This summary is NOT evidence that the log is clean - "
                "re-run with structured=False to read the raw text."
            ]
        else:
            result["warnings"] = [
                f"No log entries arrived to parse ({total_raw_lines} lines "
                f"fetched, none of them log entries). {_EMPTY_FETCH_WARNING} "
                "Re-run with structured=False to see the raw response."
            ]
    elif not parsed:
        result["warnings"] = [
            f"None of the {extracted.matched} parsed log entries matched the "
            "requested filters. The log itself parsed fine, so this reflects "
            "the filters, not a parse failure."
        ]
    return result


def _compact_logbook_entries(entries: list[Any]) -> list[dict[str, Any]]:
    """Strip logbook entries to essential fields only.

    Returns entries with only the fields in COMPACT_LOGBOOK_FIELDS,
    filtering out any non-dict entries.
    """
    return [
        {k: v for k, v in entry.items() if k in COMPACT_LOGBOOK_FIELDS}
        for entry in entries
        if isinstance(entry, dict)
    ]


class UtilityTools:
    def __init__(self, client: Any) -> None:
        self._client = client

    @staticmethod
    def _coerce_limit(
        limit: int | None,
        default: int = DEFAULT_LIMIT,
        suggestion_example: str = "50",
        param_name: str = "limit",
    ) -> int:
        """Validate a limit parameter, raising a structured tool error on failure."""
        effective = limit if limit is not None else default
        if effective < 1:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"{param_name} must be at least 1, got {effective}",
                    suggestions=[
                        f"Provide {param_name} as an integer "
                        f"(e.g., {suggestion_example})"
                    ],
                )
            )
        return min(effective, MAX_LIMIT)

    @staticmethod
    def _validate_log_level(level: str | None) -> str | None:
        if level is None:
            return None
        level_upper = level.strip().upper()
        if level_upper not in VALID_LOG_LEVELS:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid level '{level}'. Must be one of: {', '.join(VALID_LOG_LEVELS)}",
                    suggestions=["Use level='ERROR' to see only errors"],
                )
            )
        return level_upper

    @staticmethod
    def _collect_log_warnings(
        source: str,
        level: str | None,
        entity_id: str | None,
        end_time: str | None,
        slug: str | None,
        order: Literal["newest", "oldest"],
    ) -> list[str]:
        warnings: list[str] = []
        if source == "logger" and order != "newest":
            warnings.append(
                "Parameter 'order' does not apply to source='logger' "
                "(entries are sorted by integration name); ignored"
            )
        if source != "logbook" and any(p is not None for p in [entity_id, end_time]):
            ignored = [
                p
                for p, v in [("entity_id", entity_id), ("end_time", end_time)]
                if v is not None
            ]
            warnings.append(
                f"Parameters {', '.join(ignored)} only apply to source='logbook'; "
                f"ignored for source='{source}'"
            )
        if (
            source in ("logbook", "logger", "supervisor", "system_service")
            and level is not None
        ):
            warnings.append(
                "Parameter 'level' only applies to source='system' or 'error_log'; "
                f"ignored for source='{source}'"
            )
        if source not in ("supervisor", "system_service") and slug is not None:
            warnings.append(
                "Parameter 'slug' only applies to source='supervisor' or "
                f"'system_service'; ignored for source='{source}'"
            )
        return warnings

    @staticmethod
    def _validate_log_slug(source: str, slug: str | None) -> None:
        if source == "system_service":
            if not slug:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "The 'slug' parameter is required for source='system_service'",
                        suggestions=[
                            "Provide a service name, e.g. slug='supervisor' "
                            f"(allowed: {', '.join(sorted(SYSTEM_SERVICE_SLUGS))})",
                        ],
                    )
                )
            if slug not in SYSTEM_SERVICE_SLUGS:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid system_service slug '{slug}'. Must be one of: "
                        f"{', '.join(sorted(SYSTEM_SERVICE_SLUGS))}",
                        suggestions=[
                            "Pick a valid service name (e.g. 'supervisor', 'host')",
                            "For add-on container logs use source='supervisor' with "
                            + "the add-on slug instead",
                        ],
                    )
                )
        elif source == "supervisor" and not slug:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "The 'slug' parameter is required for source='supervisor'",
                    suggestions=[
                        "Provide the add-on slug, e.g. slug='core_mosquitto'",
                        "Use ha_get_addon() to list installed add-on slugs",
                    ],
                )
            )

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
                structured=structured,
                top_n=top_n,
            )
        if source == "logger":
            # logger reports per-integration levels, not time-ordered events;
            # 'order' does not apply (a warning is emitted upstream).
            return await self._get_logger_info(limit=limit, search=search)
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
        level = self._validate_log_level(level)
        warnings = self._collect_log_warnings(
            source, level, entity_id, end_time, slug, order
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
        self._validate_log_slug(source, slug)
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

    @staticmethod
    def _coerce_logbook_params(
        hours_back: int,
        limit: int | None,
        offset: int,
    ) -> tuple[int, int, int]:
        effective_limit = UtilityTools._coerce_limit(limit)
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
        """Build reproducible pagination hint string for logbook results."""
        next_offset = offset_int + effective_limit
        param_parts = [
            f"hours_back={hours_back_int}",
            f"limit={effective_limit}",
            f"offset={next_offset}",
        ]
        if entity_id:
            param_parts.append(f"entity_id={entity_id}")
        if end_time:
            param_parts.append(f"end_time={end_time}")
        if search:
            param_parts.append(f"search={search}")
        if not compact_bool:
            param_parts.append("compact=False")
        if order != "newest":
            param_parts.append(f"order={order}")
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
            end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
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
        effective_limit = self._coerce_limit(limit)

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

            if level:
                entries = [
                    e for e in entries if str(e.get("level", "")).upper() == level
                ]
                filters_applied["level"] = level

            if search:
                search_lower = search.lower()
                entries = [
                    e
                    for e in entries
                    if search_lower in str(e.get("message", "")).lower()
                    or search_lower in str(e.get("name", "")).lower()
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
        effective_top_n = self._coerce_limit(
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

    async def _get_error_log(
        self,
        limit: int | None = None,
        search: str | None = None,
        level: str | None = None,
        order: Literal["newest", "oldest"] = "newest",
        structured: bool = False,
        top_n: int | None = None,
    ) -> dict[str, Any]:
        """Fetch raw error log text (home-assistant.log, or journald).

        Container/pip installs read the plain ``home-assistant.log`` file;
        Supervisor-backed installs read HA Core's journald stream instead.

        With ``structured=True`` the raw text is collapsed into a counted,
        component-grouped summary instead (see ``_parse_error_log_structured``);
        ``limit``/``order`` do not apply in that mode (the summary ranks the
        whole fetched window by occurrence count rather than returning a
        positional slice of it), and ``top_n`` bounds it instead.
        """
        try:
            raw_log = await self._client.get_error_log()

            if structured:
                return self._build_structured_error_log(
                    raw_log or "",
                    search=search,
                    level=level,
                    top_n=top_n,
                    limit=limit,
                    order=order,
                )

            # Coerced after the structured return: the summary covers the whole
            # fetched window, so `limit` has no meaning there and validating it
            # would reject limit=0 for a parameter with no effect.
            effective_limit = self._coerce_limit(
                limit, default=DEFAULT_LOG_LIMIT, suggestion_example="100"
            )
            lines = raw_log.splitlines() if raw_log else []

            filters_applied: dict[str, str] = {}

            if level:

                def _line_has_level(ln: str, target: str) -> bool:
                    m = _LOG_LEVEL_RE.search(ln)
                    return m is not None and m.group(1).upper() == target

                lines = [ln for ln in lines if _line_has_level(ln, level)]
                filters_applied["level"] = level

            if search:
                search_lower = search.lower()
                lines = [ln for ln in lines if search_lower in ln.lower()]
                filters_applied["search"] = search

            total_lines = len(lines)
            # Always take the most-recent window (the tail of the chronological
            # file); 'order' controls only the display direction of that window.
            lines = lines[-effective_limit:]
            if order == "newest":
                lines = list(reversed(lines))

            data: dict[str, Any] = {
                "success": True,
                "source": "error_log",
                "log": "\n".join(lines),
                "total_lines": total_lines,
                "returned_lines": len(lines),
                "limit": effective_limit,
                "order": order,
                "note": "Returned the most recent log lines matching filters",
            }
            if filters_applied:
                data["filters_applied"] = filters_applied
            # The default path must not bless a fetch the structured path
            # refuses to bless: without this, `structured=False` answers an
            # empty fetch with success, an empty `log` and total_lines 0, which
            # an agent reports to the user as "no errors in the log". Keyed on
            # the raw text, so this stays an empty *fetch* — an empty *filter
            # result* is a different thing and stays distinguishable through
            # `filters_applied`.
            if not (raw_log or "").strip():
                data["warnings"] = [
                    f"The fetch returned no log content. {_EMPTY_FETCH_WARNING}"
                ]

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
                suggestions=self._addon_auth_error_suggestions(),
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
        effective_limit = self._coerce_limit(limit)

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

    async def _get_supervisor_log(
        self,
        slug: str,
        limit: int | None = None,
        search: str | None = None,
        order: Literal["newest", "oldest"] = "newest",
    ) -> dict[str, Any]:
        """Fetch add-on container logs.

        Delegates to ``HomeAssistantClient.get_addon_logs`` which branches on
        ``is_running_in_addon()``: inside the add-on container hits Supervisor
        directly at ``http://supervisor/addons/<slug>/logs`` (the HA-Core
        proxy at ``/api/hassio/addons/<slug>/logs`` rejects the Supervisor
        token there — see #1116); on non-addon installs falls back to the
        HA-Core proxy. Both paths return ``text/plain``.
        """
        effective_limit = self._coerce_limit(
            limit, default=DEFAULT_LOG_LIMIT, suggestion_example="100"
        )

        # Request a journald window sized to the caller's limit —
        # Supervisor's /logs endpoints default to their last-100-lines
        # window, which silently capped any larger limit before ?lines=
        # was plumbed through (found via #1721's e2e).
        fetch_lines = (
            max(effective_limit, SUPERVISOR_SEARCH_WINDOW_LINES)
            if search
            else effective_limit
        )

        try:
            log_text = await self._client.get_addon_logs(slug, lines=fetch_lines)

            lines = log_text.splitlines() if log_text else []

            filters_applied: dict[str, str] = {}

            if search:
                search_lower = search.lower()
                lines = [ln for ln in lines if search_lower in ln.lower()]
                filters_applied["search"] = search

            total_lines = len(lines)
            # Always take the most-recent window (the tail); 'order' controls
            # only the display direction of that window.
            lines = lines[-effective_limit:]
            if order == "newest":
                lines = list(reversed(lines))

            data: dict[str, Any] = {
                "success": True,
                "source": "supervisor",
                "slug": slug,
                "log": "\n".join(lines),
                "total_lines": total_lines,
                "returned_lines": len(lines),
                "limit": effective_limit,
                "order": order,
            }
            if filters_applied:
                data["filters_applied"] = filters_applied

            return data

        except ToolError:
            raise
        except HomeAssistantAuthError as e:
            # Listed before HomeAssistantAPIError because AuthError is a sibling,
            # not a subclass — without this explicit clause the 401 from
            # _supervisor_logs_get / _raw_request propagates raw to FastMCP and
            # surfaces without a structured `code` field.
            #
            # Suggestions branch on is_running_in_addon(): addon installs go
            # direct to Supervisor (the failure mode is a missing/rotated
            # SUPERVISOR_TOKEN), non-addon installs hit HA Core's hassio
            # proxy with the user's LLA (the failure mode is a non-admin or
            # expired LLA — SUPERVISOR_TOKEN doesn't even apply).
            exception_to_structured_error(
                e,
                context={"source": "supervisor", "slug": slug},
                suggestions=self._addon_auth_error_suggestions(),
            )
        except HomeAssistantAPIError as e:
            status = getattr(e, "status_code", None)
            if status == 400:
                # Supervisor-side rejection — not caller validation. The default
                # `exception_to_structured_error` path would map 400 →
                # VALIDATION_INVALID_PARAMETER, which reads as "caller passed
                # bad input"; a downstream proxy rejection is better modelled
                # as SERVICE_CALL_FAILED.
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        str(e),
                        context={"source": "supervisor", "slug": slug},
                        suggestions=[
                            f"Supervisor rejected the request for '{slug}' — "
                            "verify slug format or that the add-on is installed "
                            "and running",
                            "Use ha_get_addon() to list installed add-on slugs",
                            "Ensure Supervisor is available (HA OS or Supervised install)",
                        ],
                    )
                )
            if status == 404:
                first_suggestion = f"Add-on '{slug}' not found or not installed"
            else:
                first_suggestion = f"Verify add-on slug '{slug}' is correct"
            exception_to_structured_error(
                e,
                context={"source": "supervisor", "slug": slug},
                suggestions=[
                    first_suggestion,
                    "Use ha_get_addon() to list installed add-on slugs",
                    "Ensure Supervisor is available (HA OS or Supervised install)",
                ],
            )
        except (
            HomeAssistantConnectionError,
            TimeoutError,
            OSError,
        ) as e:
            exception_to_structured_error(
                e,
                context={"source": "supervisor", "slug": slug},
                suggestions=[
                    "Check Home Assistant connection",
                    f"Verify add-on slug '{slug}' is correct",
                    "Use ha_get_addon() to list installed add-on slugs",
                    "Ensure Supervisor is available (HA OS or Supervised install)",
                ],
            )
            raise  # unreachable: exception_to_structured_error always raises
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    @staticmethod
    def _addon_auth_error_suggestions() -> list[str]:
        if is_running_in_addon():
            return [
                "Verify SUPERVISOR_TOKEN is set correctly inside the add-on",
                "Reinstall the add-on if the token may have rotated",
            ]
        return [
            "Verify HOMEASSISTANT_TOKEN is a valid admin Long-Lived Access Token (Settings → Profile → Long-Lived Access Tokens)",
            "Re-create the LLAT if it has expired or been revoked",
        ]

    def _handle_system_service_api_error(
        self, e: HomeAssistantAPIError, service: str
    ) -> NoReturn:
        """Raise a structured error for a Supervisor per-service-logs failure.

        Branches on HTTP status: 403 (role/permission, addon vs. non-addon
        remediation differs), 404 (service not exposed on this HA OS
        version), else falls through to a generic Supervisor-error message.
        """
        status = getattr(e, "status_code", None)
        if status == 403:
            # In-addon: Supervisor returns 403 when the addon's hassio_role
            # is below 'manager'. Non-addon: HA Core's hassio proxy returns
            # 403 when the LLA's user lacks admin — completely different
            # remediation. Branch on the gate accordingly.
            if is_running_in_addon():
                suggestions = [
                    "Addon's hassio_role must be 'manager' or higher to "
                    + "read /<service>/logs",
                    "Verify the addon was reinstalled after the role bump "
                    + "took effect",
                ]
            else:
                suggestions = [
                    "The Long-Lived Access Token must belong to a user "
                    + "with admin privileges",
                    "Generate a new LLAT under an admin account and set "
                    + "HOMEASSISTANT_TOKEN to it",
                ]
            exception_to_structured_error(
                e,
                context={"source": "system_service", "slug": service},
                suggestions=suggestions,
            )
        if status == 404:
            exception_to_structured_error(
                e,
                context={"source": "system_service", "slug": service},
                suggestions=[
                    f"Service '{service}' not found at "
                    f"http://supervisor/{service}/logs — Supervisor may "
                    "not expose it on this HA OS version",
                    f"Allowed services: {', '.join(sorted(SYSTEM_SERVICE_SLUGS))}",
                ],
            )
        exception_to_structured_error(
            e,
            context={"source": "system_service", "slug": service},
            suggestions=[
                f"Supervisor returned an error for /{service}/logs",
                "Ensure Supervisor is available (HA OS or Supervised install)",
            ],
        )

    async def _get_system_service_log(
        self,
        service: str,
        limit: int | None = None,
        search: str | None = None,
        order: Literal["newest", "oldest"] = "newest",
    ) -> dict[str, Any]:
        """Fetch HA system-service logs from Supervisor's per-service endpoint.

        ``service`` ∈ ``SYSTEM_SERVICE_SLUGS`` (the eight Supervisor-managed
        services: supervisor, host, core, dns, audio, cli, multicast, observer).
        Caller (``ha_get_logs(source='system_service')``) validates against
        ``SYSTEM_SERVICE_SLUGS`` before dispatch. Routed through
        ``HomeAssistantClient._get_system_service_logs`` which gates on
        ``is_running_in_addon()``: addon installs hit Supervisor directly at
        ``http://supervisor/<service>/logs`` (requires ``hassio_role: manager``
        in the addon manifest), non-addon installs fall back to the HA Core
        proxy at ``/api/hassio/<service>/logs`` (requires an admin LLA).
        """
        effective_limit = self._coerce_limit(
            limit, default=DEFAULT_LOG_LIMIT, suggestion_example="100"
        )

        fetch_lines = (
            max(effective_limit, SUPERVISOR_SEARCH_WINDOW_LINES)
            if search
            else effective_limit
        )

        try:
            log_text = await self._client._get_system_service_logs(
                service, lines=fetch_lines
            )

            lines = log_text.splitlines() if log_text else []

            filters_applied: dict[str, str] = {}
            if search:
                search_lower = search.lower()
                lines = [ln for ln in lines if search_lower in ln.lower()]
                filters_applied["search"] = search

            total_lines = len(lines)
            # Always take the most-recent window (the tail); 'order' controls
            # only the display direction of that window.
            lines = lines[-effective_limit:]
            if order == "newest":
                lines = list(reversed(lines))

            data: dict[str, Any] = {
                "success": True,
                "source": "system_service",
                "slug": service,
                "log": "\n".join(lines),
                "total_lines": total_lines,
                "returned_lines": len(lines),
                "limit": effective_limit,
                "order": order,
            }
            if filters_applied:
                data["filters_applied"] = filters_applied

            return data

        except ToolError:
            raise
        except HomeAssistantAuthError as e:
            # Listed before HomeAssistantAPIError because AuthError is a sibling,
            # not a subclass — without this explicit clause the 401 from
            # _supervisor_logs_get / _raw_request propagates raw to FastMCP and
            # surfaces without a structured `code` field.
            #
            # Suggestions branch on is_running_in_addon() (see _get_supervisor_log
            # for the rationale): SUPERVISOR_TOKEN suggestions only make sense
            # inside the addon container; non-addon installs need admin-LLA hints.
            exception_to_structured_error(
                e,
                context={"source": "system_service", "slug": service},
                suggestions=self._addon_auth_error_suggestions(),
            )
        except HomeAssistantAPIError as e:
            self._handle_system_service_api_error(e, service)
        except (
            HomeAssistantConnectionError,
            TimeoutError,
            OSError,
        ) as e:
            exception_to_structured_error(
                e,
                context={"source": "system_service", "slug": service},
                suggestions=[
                    "Check Home Assistant connection",
                    "Ensure Supervisor is available (HA OS or Supervised install)",
                ],
            )
            raise  # unreachable: exception_to_structured_error always raises
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    async def eval_template(
        self, template: str, timeout: int, report_errors: bool
    ) -> dict[str, Any]:

        try:
            request_id = int(time.time() * 1000) % 1000000  # Simple unique ID

            message: dict[str, Any] = {
                "type": "render_template",
                "template": template,
                "timeout": timeout,
                "report_errors": report_errors,
                "id": request_id,
            }

            result = await self._client.send_websocket_message(message)

            if result.get("success"):
                if "event" in result and "result" in result["event"]:
                    template_result = result["event"]["result"]
                    listeners = result["event"].get("listeners", {})

                    return {
                        "success": True,
                        "template": template,
                        "result": template_result,
                        "listeners": listeners,
                        "request_id": request_id,
                        "evaluation_time": timeout,
                    }
                else:
                    return {
                        "success": True,
                        "template": template,
                        "result": result.get("result"),
                        "request_id": request_id,
                        "evaluation_time": timeout,
                    }
            else:
                error_info = result.get("error", "Unknown error occurred")
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        str(error_info)
                        if not isinstance(error_info, str)
                        else error_info,
                        context={"template": template, "request_id": request_id},
                        suggestions=[
                            "Check template syntax - ensure proper Jinja2 formatting",
                            "Verify entity_ids exist using ha_get_state()",
                            "Use default values: {{ states('sensor.temp') | float(0) }}",
                            "Check for typos in function names and entity references",
                            "Test simpler templates first to isolate issues",
                        ],
                    )
                )

        except ToolError:
            raise
        except Exception as e:
            error_str = str(e)
            suggestions = [
                "Check Home Assistant WebSocket connection",
                "Verify template syntax is valid Jinja2",
                "Try a simpler template to test basic functionality",
                "Check if referenced entities exist",
                "Ensure template doesn't exceed timeout limit",
            ]

            # Add specific suggestions for 403 errors
            if "403" in error_str and "Forbidden" in error_str:
                suggestions = [
                    "The request was blocked (403 Forbidden) - this may be caused by:",
                    "  • Reverse proxy security rules (Apache, Nginx, Traefik)",
                    "  • Rate limiting from multiple simultaneous requests",
                    "  • Complex template triggering security filters",
                    "Try simplifying the template (remove newlines, reduce complexity)",
                    "Break complex templates into multiple simpler calls",
                    "Use ha_report_issue to check Home Assistant logs for details",
                ] + suggestions

            exception_to_structured_error(
                e,
                context={"template": template},
                suggestions=suggestions,
            )
            raise  # unreachable: exception_to_structured_error always raises
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable


def register_utility_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant utility tools."""
    tools = UtilityTools(client)

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
        ] = "logbook",
        # Shared parameters
        limit: int | None = None,
        search: str | None = None,
        order: Annotated[
            Literal["newest", "oldest"],
            Field(
                description=(
                    "Sort order for time-ordered sources (logbook, system, "
                    "error_log, supervisor, system_service): 'newest' (default) "
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
        offset: Annotated[int, Field(ge=0)] = 0,
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
        - "supervisor": Add-on container logs (requires slug = add-on slug)
        - "system_service": HA-Supervisor-managed system service logs (requires
          slug ∈ {supervisor, host, core, dns, audio, cli, multicast, observer})
        - "logger": Effective log level per integration via logger/log_info (confirms logger.set_level changes took effect)

        **Prefer source='system' for triage.** It returns HA's own deduplicated
        system_log entries with counts, first_occurred and full tracebacks; of
        those only the tracebacks are unrecoverable from the structured
        error_log summary — they are present in the raw text, so structured=False
        gets them back. Its counts also run
        since each error first occurred, while structured error_log counts only
        what is inside the fetched window (reported as window_start/window_end;
        Supervisor-backed installs read a capped journald slice). Use error_log
        with structured=True for entries below system_log's WARNING+ ~50-entry
        cap, or for the per-component rollup.

        **Shared params:** limit, search (keyword filter on entries/lines; matches integration domain for source='logger')
        **Order:** order='newest' (default) returns most-recent first; order='oldest' returns chronological-first. Applies to all time-ordered sources (logbook, system, error_log, supervisor, system_service); ignored for source='logger' and for error_log with structured=True. For raw-text sources (error_log, supervisor, system_service) it sets the read direction of the most-recent window.
        **Logbook params:** hours_back, entity_id, end_time, offset, compact (default True — strips attribute dicts to save context)
        **System/error_log params:** level (ERROR, WARNING, INFO, DEBUG, CRITICAL)
        **error_log params:** structured, top_n. In structured mode `search`
            matches the message and logger name only, whereas on the raw path it
            matches the whole line; `limit`/`order` do not apply, and issues are
            ranked by count, then severity, then recency.
        **Supervisor params:** slug = add-on slug, e.g. "core_mosquitto" (use
            ha_get_addon() to list installed slugs)
        **System-service params:** slug = service name. The slug "supervisor"
            here means the Supervisor service's own logs, NOT an add-on with
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

    @mcp.tool(
        tags={"Utilities"},
        annotations={
            "openWorldHint": False,
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Evaluate Template",
        },
    )
    @log_tool_usage
    async def ha_eval_template(
        template: str, timeout: int = 3, report_errors: bool = True
    ) -> dict[str, Any]:
        """
        Evaluate Jinja2 templates using Home Assistant's template engine.

        This tool allows testing and debugging of Jinja2 template expressions that are commonly used in
        Home Assistant automations, scripts, and configurations. It provides real-time evaluation with
        access to all Home Assistant states, functions, and template variables.

        **When NOT to use this for automation/script logic:**
        Templates have legitimate uses (notification bodies, dynamic `data.*` values,
        debugging existing templates), but `condition:` / `trigger:` positions and
        action service names are better expressed as native HA constructs:
        native constructs are schema-validated at config load and surface
        structural errors loudly, whereas equivalent template logic only errors
        at runtime — and a template that renders a non-truthy value is silently
        treated as false.
        Prefer:
        - `condition: numeric_state` over `{{ states('x') | float > N }}`
        - `condition: state` over `{{ is_state(...) }}`
        - `condition: time` / `condition: sun` over `now().hour` / `is_state('sun.sun', ...)`
        - Native `for:` field on state/numeric_state triggers and state conditions over
          `{{ now() - X.last_changed > timedelta(...) }}` duration math
        - `choose` action over templated `service:` / `action:` strings
        See `ha_get_skill_guide` (best-practices skill) for the full anti-pattern list.

        **When to use (reach for this tool, don't compute it yourself):**
        Any one-shot question whose answer is DERIVED from current HA state — an
        average/sum/min/max across sensors, a count of entities matching a
        condition, a boolean comparison, or a rendered message with live values.
        One render call beats fetching N states and doing the math yourself, and
        it is the canonical way to *test* a template before embedding it. This is
        for one-shot answers and template testing only — NOT for putting templates
        into automation logic; for `condition:` / `trigger:` positions native
        constructs win.
        - "average temperature across the bedroom sensors"
          -> `{{ ([states('sensor.a'), states('sensor.b')] | map('float', 0) | sum) / 2 }}`
        - "how many lights are on"
          -> `{{ states.light | selectattr('state', 'eq', 'on') | list | count }}`
        NOT for a plain single-entity value ("what's the state of X") — that is
        `ha_get_state` / `ha_search`; rendering `{{ states('X') }}` there is over-use.

        **Parameters:**
        - template: The Jinja2 template string to evaluate
        - timeout: Maximum evaluation time in seconds (default: 3)
        - report_errors: Whether to return detailed error information (default: True)

        **Common Template Functions:**

        **State Access:**
        ```jinja2
        {{ states('sensor.temperature') }}              # Get entity state value
        {{ states.sensor.temperature.state }}           # Alternative syntax
        {{ state_attr('light.bedroom', 'brightness') }} # Get entity attribute
        {{ is_state('light.living_room', 'on') }}       # Check if entity has specific state
        ```

        **Numeric Operations:**
        ```jinja2
        {{ states('sensor.temperature') | float(0) }}   # Convert to float with default
        {{ states('sensor.humidity') | int(0) }}        # Convert to integer with default
        {{ (states('sensor.temp') | float(0) + 5) | round(1) }} # Math operations
        ```

        **Time and Date:**
        ```jinja2
        {{ now() }}                                     # Current datetime
        {{ now().strftime('%H:%M:%S') }}               # Format current time
        {{ as_timestamp(now()) }}                      # Convert to Unix timestamp
        {{ now().hour }}                               # Current hour (0-23)
        {{ now().weekday() }}                          # Day of week (0=Monday)
        ```

        **Conditional Logic (for display strings — not for `condition:` positions):**
        ```jinja2
        {{ 'Day' if now().hour < 18 else 'Night' }}    # Ternary operator
        {% if is_state('alarm_control_panel.home', 'armed_away') %}
          Alarm is armed
        {% else %}
          Alarm is disarmed
        {% endif %}
        ```

        **Lists and Loops:**
        ```jinja2
        {% for entity in states.light %}
          {{ entity.entity_id }}: {{ entity.state }}
        {% endfor %}

        {{ states.light | selectattr('state', 'eq', 'on') | list | count }} # Count on lights
        ```

        **String Operations:**
        ```jinja2
        {{ states('sensor.weather') | title }}         # Title case
        {{ 'Hello ' + states('input_text.name') }}     # String concatenation
        {{ states('sensor.data') | regex_replace('pattern', 'replacement') }}
        ```

        **Device and Area Functions:**
        ```jinja2
        {{ device_entities('device_id_here') }}        # Get entities for device
        {{ area_entities('living_room') }}             # Get entities in area
        {{ device_id('light.bedroom') }}               # Get device ID for entity
        ```

        **Common Use Cases (legitimate template positions):**

        **Dynamic Service Data:**
        ```jinja2
        # Dynamic brightness based on time
        {{ 255 if now().hour < 22 else 50 }}

        # Message with current values
        "Temperature is {{ states('sensor.temp') }}°C, humidity {{ states('sensor.humidity') }}%"
        ```

        **Examples:**

        **Test basic state access:**
        ```python
        ha_eval_template("{{ states('light.living_room') }}")
        ```

        **Test a string expression (e.g. for a notification body):**
        ```python
        ha_eval_template("{{ 'Day' if now().hour < 18 else 'Night' }}")
        ```

        **Test mathematical operations:**
        ```python
        ha_eval_template("{{ (states('sensor.temperature') | float(0) + 5) | round(1) }}")
        ```

        **Test entity counting:**
        ```python
        ha_eval_template("{{ states.light | selectattr('state', 'eq', 'on') | list | count }}")
        ```

        **IMPORTANT NOTES:**
        - Templates have access to all current Home Assistant states and attributes
        - Use this tool to test templates before using them in automations or scripts
        - Template evaluation respects Home Assistant's security model and timeouts
        - Complex templates may affect Home Assistant performance - keep them efficient
        - Use default values (e.g., `| float(0)`) to handle missing or invalid states

        **For template documentation:** https://www.home-assistant.io/docs/configuration/templating/
        """
        return await tools.eval_template(template, timeout, report_errors)
