"""Continuation for a component-backed whole-instance Recorder page."""

from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, FiniteFloat

LOGBOOK_CURSOR_PREFIX = "logbook:"


class LogbookCursor(BaseModel):
    hours_back: int = Field(default=1, ge=1)
    total: int = Field(ge=0)
    end: datetime
    order: Literal["newest", "oldest"]
    search: str | None
    after: tuple[FiniteFloat, Literal[0, 1], int]
    seen: int = Field(ge=0)
    skip: int = Field(ge=0)
    # Older cursors did not record this. Let their raw continuation run to exhaustion.
    open_at_count: bool = True

    def token(self) -> str:
        return LOGBOOK_CURSOR_PREFIX + self.model_dump_json()

    @classmethod
    def parse(cls, value: str) -> LogbookCursor:
        return cls.model_validate_json(value.removeprefix(LOGBOOK_CURSOR_PREFIX))


def logbook_windows(
    start: datetime, end: datetime, newest: bool = False
) -> Iterator[tuple[datetime, datetime]]:
    """Visit the requested window one hour at a time without accumulating rows."""
    hour = timedelta(hours=1)
    while start < end:
        if newest:
            boundary = max(start, end - hour)
            yield boundary, end
            end = boundary
        else:
            boundary = min(end, start + hour)
            yield start, boundary
            start = boundary


async def read_matched_page(
    fetch: Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]],
    command: dict[str, Any],
    request: dict[str, Any],
    start: datetime,
    end: datetime,
    cursor: LogbookCursor | None,
) -> dict[str, Any]:
    """Collect matching identities during the count; hydrate only this page."""
    newest = request["order"] == "newest"
    limit = request["limit"]
    skip = request["skip"]
    keys: list[Any] = []
    total = 0
    remaining_hours = False
    overlap = timedelta(microseconds=1)
    scan_start, scan_end = start, end
    if cursor:
        anchor = datetime.fromtimestamp(cursor.after[0], UTC)
        if newest:
            scan_end = min(end, anchor + overlap)
        else:
            scan_start = max(start, anchor - overlap)
    for lo, hi in logbook_windows(scan_start, scan_end, newest=newest):
        if (
            cursor
            and len(keys) == limit
            and (not cursor.open_at_count or total > request["skip"] + limit)
        ):
            remaining_hours = True
            break
        native_start = lo - overlap if lo > start else lo
        page = {
            **request,
            "count_range": [lo.timestamp(), hi.timestamp()],
            "skip": skip,
        }
        if len(keys) < limit:
            page["collect_keys"] = limit - len(keys)
        batch = await fetch(
            {
                "start_time": native_start.isoformat(),
                "end_time": hi.isoformat(),
            },
            page,
        )
        total += batch["total"]
        skip = max(0, skip - batch["total"])
        keys.extend(batch["keys"])
    skipped = request["skip"] - skip
    entries: list[Any] = []
    after = None
    if keys:
        rendered = await fetch(command, {**request, "keys": keys})
        entries = rendered["entries"]
        if remaining_hours or total > skipped + len(entries):
            after = rendered["after"]
    return {
        "entries": entries,
        "after": after,
        "total": total,
        "matched": skipped + len(entries),
        "skip": skip,
    }
