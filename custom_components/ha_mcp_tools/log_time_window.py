"""Filter Core and journal records by event time and page whole timestamp groups."""

import math
from datetime import UTC, datetime, timedelta
from itertools import groupby
from typing import Any

from homeassistant.const import KEY_DATA_LOGGING
from homeassistant.core import HomeAssistant
from homeassistant.helpers.hassio import is_hassio

from .supervisor_log import _prepare as read_journal

_MAX_FILE_BYTES = 1_000_000


def _lines(text: str) -> list[str]:
    """Lines as the journal and the file count them: split at newlines only."""
    return text.removesuffix("\n").split("\n") if text else []


def _requests(
    name: str, start: datetime, end: datetime, count: int, raw: bool, skip: int
) -> list[dict[str, str]]:
    """The journal reads for one source.

    A timeline makes the read Core's log tool makes; a raw page makes the page
    and a one-entry probe that says whether more follows.

    Core's tool (``hassio/llm.py``) ends its range a whole second after the
    window and reads Core and Supervisor in the plain format.
    """
    begin = f"realtime={start.timestamp():.6f}:"
    if raw:
        span = f"{begin}{end.timestamp():.6f}"
        return [
            {"source": name, "range": f"{span}:{skip}:{count}"},
            {"source": name, "range": f"{span}:{skip + count}:1"},
        ]
    stop = math.floor(end.timestamp()) + 1
    return [
        {
            "source": name,
            "range": f"{begin}{stop}:{skip}:{count}",
            "format": "plain" if name in {"core", "supervisor"} else "verbose",
        }
    ]


async def _file_source(hass: HomeAssistant, *page: Any) -> dict[str, Any]:
    """Core's log file as one source; a file that cannot be read is its error."""
    try:
        return await hass.async_add_executor_job(_file_page, *page)
    except OSError as exc:
        return {
            "error": f"Core's log file could not be read ({type(exc).__name__}).",
            "status": 404 if isinstance(exc, FileNotFoundError) else None,
        }


async def _source(
    hass: HomeAssistant,
    name: str,
    start: datetime,
    end: datetime,
    count: int,
    raw: bool,
    skip: int,
) -> dict[str, Any]:
    if name == "core" and (path := hass.data.get(KEY_DATA_LOGGING)):
        return await _file_source(hass, path, start, end, count, skip, raw)
    wanted = entries = count - 1 if raw else count
    while True:
        reads = _requests(name, start, end, entries, raw, skip)
        reply = await read_journal(hass, reads[0])
        if not reply.get("byte_limited") or entries == 1:
            break
        entries //= 2
    if reply.get("unavailable") and not is_hassio(hass):
        return {"error": "This installation has no Supervisor journal.", "status": 404}
    # Core 2026.11 fallback: remove when the minimum Home Assistant is 2026.12.
    if reply.get("unavailable"):
        return {**reply, "requests": _requests(name, start, end, wanted, raw, skip)}
    if "text" not in reply:
        return reply
    lines = _lines(reply["text"])
    if entries < wanted:
        return {"lines": lines, "byte_limited": True, "entries": entries}
    if raw:
        probe = await read_journal(hass, reads[1])
        if "text" not in probe:
            return probe
        return {
            "lines": lines,
            "has_more": bool(probe.get("first_cursor") or probe["text"]),
        }
    return {"lines": lines}


def _merge(
    batches: list[tuple[str, list[str], bool]],
    start: datetime,
    end: datetime,
    limit: int,
    search: str,
    errors: dict[str, str],
) -> dict[str, Any]:
    """Merge the sources as Core's log tool does (``system_log/llm.py``).

    A source whose bounded read holds no record from ``start`` on is put in
    ``errors``: the read gives no later time to continue from.
    """
    from homeassistant.components.system_log.llm import (
        _LOG_QUERY_ORDER_MARGIN,
        _parse_log_records,
    )

    rows: list[tuple[datetime, str, str]] = []
    warnings: list[str] = []
    complete_end = None
    for name, lines, limited in batches:
        records = _parse_log_records(lines)
        if limited:
            times = [row.time for row in records if row.time >= start]
            if not times:
                errors[name] = (
                    f"The {len(lines)} lines read from start_time continue a record "
                    "that began before it. Set a later start_time to continue this source."
                )
                continue
            boundary = min(
                records[-1].time,
                max(row.time for row in records) - _LOG_QUERY_ORDER_MARGIN,
            )
            first = min(times)
            if boundary <= first:
                # Keep the first time's records, so the next page starts later.
                boundary = first + timedelta(milliseconds=1)
                warnings.append(
                    f"The {name} records at {first.isoformat()} fill one read of "
                    f"{len(lines)} lines; anything more at that instant is not returned."
                )
            complete_end = min(complete_end or boundary, boundary)
        rows.extend(
            (row.time, name, row.message)
            for row in records
            if start <= row.time <= end and search.casefold() in row.message.casefold()
        )
    rows.sort(key=lambda row: row[0])
    selected: list[dict[str, Any]] = []
    next_time = complete_end
    for moment, group in groupby(rows, key=lambda row: row[0]):
        if complete_end and moment >= complete_end:
            break
        if len(selected) >= limit:
            next_time = moment
            break
        selected.extend(
            {"source": name, "time": stamp.isoformat(), "message": message}
            for stamp, name, message in group
        )
    result: dict[str, Any] = {"records": selected, "returned_records": len(selected)}
    if warnings:
        result["warnings"] = warnings
    if next_time and next_time <= end:
        result["next_start_time"] = next_time.isoformat()
    result["has_more"] = "next_start_time" in result
    return result


def _file_page(
    path: str, start: datetime, end: datetime, count: int, skip: int, raw: bool
) -> dict[str, Any]:
    from homeassistant.components.system_log.llm import (
        _LOG_QUERY_ORDER_MARGIN,
        _RECORD_START,
    )

    selected: list[str] = []
    size = 0
    inside: bool | None = None
    warnings: list[str] = []
    with open(path, "rb") as file:
        while block := file.readline(_MAX_FILE_BYTES + 1):
            if len(block) > _MAX_FILE_BYTES:
                return {"error": "A file log line exceeds HA-MCP's byte limit."}
            line = block.decode(errors="replace").rstrip("\r\n")
            if match := _RECORD_START.match(line):
                stamp = datetime.fromisoformat(match[1])
                inside = stamp.tzinfo is not None and start <= stamp <= end
                if stamp.tzinfo and stamp > end + _LOG_QUERY_ORDER_MARGIN:
                    break
            elif inside is None and raw and not warnings:
                warnings.append(
                    "File text before its first timestamp cannot be placed in an exact time window. An ordinary raw read includes it."
                )
            if not inside:
                continue
            if skip:
                skip -= 1
                continue
            size += len(block)
            if size > _MAX_FILE_BYTES:
                warnings.append("HA-MCP's byte limit shortened this file window.")
                return {"lines": selected, "byte_limited": True, "warnings": warnings}
            selected.append(line)
            if len(selected) == count:
                break
    return {"lines": selected, "warnings": warnings}


def _raw_page(msg: dict[str, Any], batch: dict[str, Any]) -> dict[str, Any]:
    # A journal batch is exactly the page; a file batch holds one line more,
    # which says whether another page follows.
    lines = batch["lines"]
    whole = "has_more" in batch or "entries" in batch
    selected = [
        line
        for line in (lines if whole else lines[: msg["limit"]])
        if msg["search"].casefold() in line.casefold()
    ]
    result = {
        "log": "\n".join(selected),
        "returned_lines": len(selected),
        "has_more": batch.get("has_more", len(lines) > msg["limit"]),
    }
    if batch.get("byte_limited"):
        result.update(
            has_more=True,
            next_offset=msg["offset"] + batch.get("entries", len(lines)),
        )
    if batch.get("warnings"):
        result["warnings"] = batch["warnings"]
    return result


def _window(msg: dict[str, Any]) -> tuple[datetime, datetime] | None:
    try:
        start, end = (
            datetime.fromisoformat(msg[key]) for key in ("start_time", "end_time")
        )
    except ValueError:
        return None
    if not start.tzinfo or not end.tzinfo or end < start:
        return None
    return start.astimezone(UTC), end.astimezone(UTC)


# Core 2026.11 fallback: remove when the minimum Home Assistant is 2026.12.
def _given(msg: dict[str, Any], name: str) -> dict[str, Any] | None:
    """Journal text the caller read itself, where this install could not."""
    if name not in msg.get("journal_text", {}):
        return None
    return {
        "lines": _lines(msg["journal_text"][name]),
        "byte_limited": name in msg.get("journal_limited", ()),
    }


async def _prepare(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    from homeassistant.components.system_log.llm import LOG_QUERY_READ_LINES

    if not (window := _window(msg)):
        return {"error": "Use ordered ISO timestamps with UTC offsets.", "status": 400}
    start, end = window
    raw = msg["raw"]
    names = msg["sources"]
    if raw and len(names) != 1:
        return {"error": "Single-source raw reads require one source."}
    count = msg["limit"] + 1 if raw else LOG_QUERY_READ_LINES
    batches = []
    errors: dict[str, str] = {}
    requests: dict[str, list[dict[str, str]]] = {}
    statuses: set[int | None] = set()
    for name in names:
        batch = _given(msg, name) or await _source(
            hass, name, start, end, count, raw, msg["offset"]
        )
        if batch.get("unavailable"):
            requests[name] = batch["requests"]
        elif "lines" not in batch:
            errors[name] = batch["error"]
            statuses.add(batch.get("status"))
        elif raw:
            return _raw_page(msg, batch)
        else:
            lines = batch["lines"]
            batches.append(
                (name, lines, bool(batch.get("byte_limited")) or len(lines) >= count)
            )
    if requests:
        # Core 2026.11 fallback (the caller makes these reads): remove when the
        # minimum Home Assistant is 2026.12.
        return {"unavailable": True, "requests": requests}
    if errors and not batches:
        text = " ".join(f"{name}: {error}" for name, error in errors.items())
        return {"error": text, "status": statuses.pop() if len(statuses) == 1 else None}
    result = _merge(batches, start, end, msg["limit"], msg["search"], errors)
    if errors:
        result["errors"] = errors
    return result


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    return [
        (
            {
                vol.Required("type"): "ha_mcp_tools/log_time_window",
                vol.Required("sources"): vol.All(
                    [vol.Match(r"^(?:app:)?[\w][\w.-]*\Z")], vol.Length(min=1, max=12)
                ),
                vol.Required("start_time"): str,
                vol.Required("end_time"): str,
                vol.Required("limit"): vol.All(int, vol.Range(min=1, max=500)),
                vol.Optional("search", default=""): str,
                vol.Optional("raw", default=False): bool,
                # Core 2026.11 fallback: remove when the minimum Home Assistant is 2026.12.
                vol.Optional("journal_text"): vol.All(
                    {str: vol.All(str, vol.Length(max=1_000_000))}, vol.Length(max=12)
                ),
                vol.Optional("journal_limited"): [str],
                vol.Optional("offset", default=0): vol.All(int, vol.Range(min=0)),
            },
            lambda hass, msg, **result: result,
            _prepare,
        )
    ]
