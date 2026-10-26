"""Structured summarisation of Home Assistant's raw error log.

``ha_get_logs(source='error_log')`` returns raw text by default. A busy
instance produces a 50-200 KB log in which the same handful of errors repeat
thousands of times, so the raw form can exhaust an agent's context while
conveying very little. This module collapses it into counted, deduplicated,
component-grouped issues.

It also owns the window/pagination unit the two response paths share: how
large a window one fetch asks for, and how the response describes where that
window sat and how to read further back.

Split out of ``tools_utility`` under `.gemini/styleguide.md` § Tool
Consolidation and Module Size: neither parsing nor window arithmetic touches
the client or tool plumbing.
"""

import re
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple

from ..client.error_log_journald import cursor_token
from ..client.rest_client import MIN_LOG_WINDOW_LINES
from .log_common import (
    DEFAULT_LOG_LIMIT,
    MAX_LIMIT,
    SUPERVISOR_SEARCH_WINDOW_LINES,
    _coerce_limit,
)

# Full HA log line, e.g.
# "2026-05-27 10:15:23.456 ERROR (MainThread) [homeassistant.components.zha] msg"
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


# Extra old lines fetched behind the caller's limit so a record whose header
# sits just outside the visible window still arrives whole. The wider fetch is
# paid on every raw read; a traceback longer than this is caught by the
# refetch-on-leading-continuation path instead.
_ERROR_LOG_HEADER_LOOKBACK_LINES = 200


@dataclass(frozen=True)
class _LogRecord:
    """One log record: a header line plus its continuation lines.

    ``header`` is ``None`` for a leading partial record — lines at the oldest
    edge of a window whose header fell outside it. ``start_index`` is the
    record's first line in the fetched window; paging resumes from it, which
    keeps a record whole across pages where ``offset`` counts lines.
    """

    lines: list[str]
    start_index: int
    header: str | None
    level: str | None

    @property
    def line_count(self) -> int:
        """Number of raw lines in the record."""
        return len(self.lines)


@dataclass(frozen=True)
class _TruncatedRecord:
    """The one over-long record a page had to cut, with its original size."""

    total_lines: int
    returned_lines: int


@dataclass(frozen=True)
class _RecordPage:
    """Records selected for one response, in display order."""

    records: list[_LogRecord]
    lines: list[str]
    oldest_index: int | None
    truncated: _TruncatedRecord | None
    leading_partial: bool


def _split_log_records(lines: list[str]) -> list[_LogRecord]:
    """Group window lines into records: a header plus its continuation lines.

    Blank lines stay inside the record: Python prints chained exceptions with
    blank lines around the separator, so a blank boundary would cut a
    traceback. Lines before the first header are one leading partial record.
    """
    records: list[_LogRecord] = []
    current: list[str] = []
    start = 0
    header: str | None = None
    level: str | None = None
    for index, line in enumerate(lines):
        clean = _strip_ansi(line).strip()
        match = _HA_LOG_LINE_RE.match(clean)
        if match:
            if current:
                records.append(_LogRecord(current, start, header, level))
            current = [line]
            start = index
            header = clean
            level = match.group(2).upper()
        else:
            if not current:
                start = index
            current.append(line)
    if current:
        records.append(_LogRecord(current, start, header, level))
    if (
        records
        and records[0].header is None
        and all(not _strip_ansi(line).strip() for line in records[0].lines)
    ):
        # A leading partial made only of separator blanks carries nothing.
        records.pop(0)
    return records


def _filter_log_records(
    records: list[_LogRecord], search: str | None, level: str | None
) -> tuple[list[_LogRecord], dict[str, str]]:
    """Filter records as units: level on the header, search on any line."""
    filtered = records
    filters_applied: dict[str, str] = {}
    if level:
        target = level.upper()
        filtered = [record for record in filtered if record.level == target]
        filters_applied["level"] = level
    if search:
        needle = search.lower()
        filtered = [
            record
            for record in filtered
            if any(needle in _strip_ansi(line).lower() for line in record.lines)
        ]
        filters_applied["search"] = search
    return filtered, filters_applied


def _cut_record_lines(record: _LogRecord, limit: int) -> list[str]:
    """The ``limit`` lines of an over-long record that a page keeps.

    The header carries the logger and the message, and the last line carries
    the exception; the frames between them are the least informative part, so
    a cut record keeps both ends instead of just its top.
    """
    if limit <= 1:
        return record.lines[:limit]
    return [record.lines[0], *record.lines[-(limit - 1) :]]


def _select_error_log_page(
    records: list[_LogRecord],
    limit: int,
    order: Literal["newest", "oldest"],
) -> _RecordPage:
    """Take the most recent records that fit ``limit`` lines.

    Records are never split to fit, apart from the single newest record when
    it alone exceeds the limit. That one keeps its header plus the last
    ``limit - 1`` lines, so a match in the exception line survives the cut.
    ``order`` controls display direction only, as before: newest-first, or
    chronological within the selected window.
    """
    selected: list[_LogRecord] = []
    used = 0
    truncated: _TruncatedRecord | None = None
    for record in reversed(records):
        if used + record.line_count <= limit:
            selected.append(record)
            used += record.line_count
            continue
        if not selected and limit > 0:
            selected.append(
                _LogRecord(
                    _cut_record_lines(record, limit),
                    record.start_index,
                    record.header,
                    record.level,
                )
            )
            truncated = _TruncatedRecord(record.line_count, limit)
        break

    if order == "oldest":
        selected.reverse()
    lines = [line for record in selected for line in record.lines]
    oldest_index = min((record.start_index for record in selected), default=None)
    leading_partial = any(record.header is None for record in selected)
    return _RecordPage(
        records=selected,
        lines=lines,
        oldest_index=oldest_index,
        truncated=truncated,
        leading_partial=leading_partial,
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


# Log window the structured error-log summary reads. Bounded replacement for
# the unconditional 20,000-line fetch that hung Supervisor-backed installs
# (#2279). Deliberately conservative and aligned with
# SUPERVISOR_SEARCH_WINDOW_LINES: structured=True was the exact call that hung,
# and no timing exists for the reporter's hardware, so the depth is a guess
# either way — `offset` pages deeper when the summary needs more history.
STRUCTURED_ERROR_LOG_WINDOW_LINES = 2000

# Appended to the last error-log page. An offset past the start of a journald
# journal clamps to the oldest entries rather than returning nothing, so the
# final page can repeat content an earlier one already showed.
_HISTORY_START_NOTE = (
    "This window reached the start of the available history; it may overlap an "
    "earlier page, since an offset past the start clamps to the oldest entries."
)


@dataclass(frozen=True)
class _ErrorLogWindow:
    """The log window one ``get_error_log`` fetch consumed.

    ``has_more`` comes from the client, which is the only layer that can
    establish it (a full journald window proves nothing on its own — see
    ``_journald_error_log_page``).

    ``offset`` is the caller's paging anchor: an integer on container/pip
    installs (text LINES back from the newest one), and either an integer or
    the opaque cursor token a previous journald page returned. A token page is
    anchored on Supervisor's ``X-First-Cursor`` plus the entries a page
    consumed, so lines logged between two calls cannot shift it;
    ``first_cursor`` carries the raw cursor that the next token is built from.
    """

    offset: int | str
    fetch_lines: int
    raw_line_count: int
    has_more: bool
    first_cursor: str | None = None

    def __post_init__(self) -> None:
        # Guards the two shapes that make paging non-terminating or
        # nonsensical. Internal type with a single construction site, so this
        # can only fire on a code bug, never on caller input.
        if self.fetch_lines < 1:
            raise ValueError(f"fetch_lines must be at least 1, got {self.fetch_lines}")
        if isinstance(self.offset, int) and self.offset < 0:
            raise ValueError(f"offset must not be negative, got {self.offset}")


def _error_log_window_lines(
    limit: int | None,
    search: str | None,
    level: str | None,
    structured: bool,
) -> int:
    """Number of raw log lines a single error-log fetch requests.

    Bounded on every path (#2279). The structured summary reads a fixed
    deep window; the raw path reads the caller's limit plus a header lookback
    (``_ERROR_LOG_HEADER_LOOKBACK_LINES``), widened to the search window when
    ``level`` or ``search`` is set, since both filter client-side over the
    fetched text and need history behind the limit to find matches in (the
    same widening ``_get_supervisor_log`` applies for ``search``; it has no
    ``level`` parameter). Floored at the client's
    minimum window so the recorded size matches the one actually served.
    """
    if structured:
        # `limit` is deliberately not coerced here: it does not apply to the
        # summary, and validating it would reject limit=0 for a parameter
        # with no effect. On the raw path it is coerced here AND again in
        # `_build_raw_error_log` — the same pure validator, called where
        # each needs the value.
        return STRUCTURED_ERROR_LOG_WINDOW_LINES
    effective_limit = _coerce_limit(
        limit, default=DEFAULT_LOG_LIMIT, suggestion_example="100"
    )
    if search or level:
        return max(effective_limit, SUPERVISOR_SEARCH_WINDOW_LINES)
    # Raw reads group lines into records, so the window must reach behind the
    # caller's limit for the header of the oldest visible record.
    return max(effective_limit + _ERROR_LOG_HEADER_LOOKBACK_LINES, MIN_LOG_WINDOW_LINES)


def _next_page_step(window: _ErrorLogWindow, oldest_index: int | None) -> int:
    """How far back the next page's ``offset`` sits from this one's.

    The next page resumes just before the oldest returned record's first line
    (its header when whole). On container and pip installs, where ``offset``
    counts lines, that keeps every record whole across pages. On journald
    ``offset`` counts entries, so a line-counted step is spent faster than it
    was measured and can overshoot past part of the history — the mismatch
    described on ``_ErrorLogWindow``. Stepping by *record* count instead was
    tried and rejected: one journald entry can carry several records and one
    record can span entries, so the count under-reached and pages repeated.
    The cap at the window size bounds the overshoot to one window. Nothing
    shown means nothing to resume before, so the whole window is consumed.
    """
    if oldest_index is None:
        return window.fetch_lines
    return min(window.raw_line_count - oldest_index, window.fetch_lines)


def _reopen_at_record_end(window: _ErrorLogWindow, record: _LogRecord) -> int:
    """Step that puts the next page's newest edge on ``record``'s last line.

    A held-back leading tail has to be re-fetched with its header, and the
    window width never changes, so the next page must start at the record's
    own last line rather than just before the oldest record returned. Capped
    like every other step; a record spanning the whole window leaves nothing
    to anchor to, so the window is consumed instead.
    """
    reanchor = window.raw_line_count - record.start_index - record.line_count
    if reanchor <= 0:
        return window.fetch_lines
    return min(reanchor, window.fetch_lines)


def _build_error_log_pagination_hint(
    next_offset: int | str,
    limit: int | None,
    search: str | None,
    level: str | None,
    order: Literal["newest", "oldest"],
    structured: bool,
    top_n: int | None,
) -> str:
    """Build the reproducible next-page call for source='error_log'.

    String values are quoted: the hint is meant to be copied back as a
    call, and an unquoted ``search=issue 7`` is not one.
    """
    parts = ["source='error_log'"]
    if structured:
        parts.append("structured=True")
        if top_n is not None:
            parts.append(f"top_n={top_n}")
    else:
        parts.append(f"limit={limit}")
        if order != "newest":
            parts.append(f"order={order!r}")
    if level:
        parts.append(f"level={level!r}")
    if search:
        parts.append(f"search={search!r}")
    parts.append(f"offset={next_offset!r}")
    return (
        "Older entries remain behind this window. To read further back, "
        f"use: ha_get_logs({', '.join(parts)})"
    )


def _attach_error_log_pagination(
    data: dict[str, Any],
    window: _ErrorLogWindow,
    step: int,
    limit: int | None,
    search: str | None,
    level: str | None,
    order: Literal["newest", "oldest"],
    structured: bool,
    top_n: int | None,
    unreturned_matches: int = 0,
    cursor_skip: int | None = None,
    older_matches_remain: bool = False,
) -> None:
    """Add the offset/has_more contract to an error-log response in place.

    Two anchors are possible and the window decides which one is used. A
    journald page carries Supervisor's cursor for the oldest entry it fetched,
    so ``cursor_skip`` (the entries this page consumed from that cursor) turns
    into a token naming the exact resume point: every page resumes exactly,
    whether or not it returned its whole window. The structured summary
    consumes its whole window, so it passes a skip of zero and its token
    resumes behind the window; it accepts those tokens back on the next call.
    Installs that read a whole log file keep the integer line step.

    ``has_more`` means "matches remain"; it is not limited to history behind
    this window. On a token page that returned only part of its window, the
    remaining matches are older records inside that same window: ``has_more``
    stays true and the token resumes just behind the oldest returned record, so
    the next window is strictly shorter and the walk ends. An integer offset
    cannot express that (a fetch past the journal's start clamps onto the same
    oldest window forever), so there the unreturned matches get a raise-the-
    limit hint instead. The hint never promises more than one response can
    deliver: past ``MAX_LIMIT`` it names the cap and asks the caller to narrow
    the match set.
    """
    data["offset"] = window.offset
    data["has_more"] = window.has_more
    # A token page that returned only part of its window still has matches
    # behind it. Its resume token names an entry inside this window rather than
    # a point past the journal's start, so the next page is strictly shorter and
    # the walk terminates: it can page on, unlike an integer offset, which would
    # clamp onto the same oldest window forever.
    continues_inside_window = (
        not window.has_more
        and older_matches_remain
        and isinstance(window.offset, str)
        and cursor_skip is not None
        and bool(window.first_cursor)
    )
    if window.has_more or continues_inside_window:
        next_offset: int | str
        if cursor_skip is not None and window.first_cursor:
            next_offset = cursor_token(window.first_cursor, cursor_skip)
        elif isinstance(window.offset, int):
            next_offset = window.offset + step
        else:
            # A token page always carries the block's cursor, so this is
            # unreachable in practice; repeating the caller's token would loop
            # forever on the same page, which is worse than failing loudly.
            raise ValueError("a page with older history must carry its block's cursor")
        data["has_more"] = True
        data["next_offset"] = next_offset
        data["pagination_hint"] = _build_error_log_pagination_hint(
            next_offset,
            limit,
            search,
            level,
            order,
            structured,
            top_n,
        )
        return
    if isinstance(window.offset, int) and window.offset and window.raw_line_count:
        # Integer offsets clamp to the oldest entries, so a terminal page can
        # repeat an earlier one. A cursor token names the exact resume point and
        # is trimmed at the journal's head, so it never overlaps.
        data["note"] = f"{data.get('note', '')} {_HISTORY_START_NOTE}".strip()
    if unreturned_matches > 0:
        total = data.get("total_lines", unreturned_matches)
        if total > MAX_LIMIT:
            # No limit can return this window in one response, so promising one
            # would send the caller back with the same unsatisfiable request.
            data["pagination_hint"] = (
                f"{unreturned_matches} more matching lines remain inside this "
                f"window (no older history exists behind it), which holds "
                f"{total} in total. A single response is capped at limit="
                f"{MAX_LIMIT}, so that call returns {MAX_LIMIT} of them; narrow "
                "the match set — a more specific 'search' or a 'level' filter — "
                "to read the rest."
            )
        else:
            data["pagination_hint"] = (
                f"{unreturned_matches} more matching lines remain inside this "
                f"window (no older history exists behind it). Repeat the call "
                f"with limit={total} to retrieve them in one response."
            )
