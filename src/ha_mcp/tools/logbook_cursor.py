"""Continuation for a component-backed whole-instance Recorder page."""

from collections.abc import Awaitable, Callable, Iterator
from datetime import datetime, timedelta
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


type PageFetch = Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]


async def read_matched_page(
    first_fetch: Callable[
        [dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any] | list[Any] | None]
    ],
    fetch: PageFetch,
    command: dict[str, Any],
    request: dict[str, Any],
    start: datetime,
    end: datetime,
) -> dict[str, Any] | None:
    """Collect matching identities during the initial count; hydrate only this page."""
    newest = request["order"] == "newest"
    limit = request["limit"]
    skip = request["skip"]
    keys: list[Any] = []
    total = 0
    for index, (lo, hi) in enumerate(logbook_windows(start, end, newest=newest)):
        native_start = lo - timedelta(microseconds=1) if lo > start else lo
        window = {"start_time": native_start.isoformat(), "end_time": hi.isoformat()}
        page = {
            **request,
            "count_range": [lo.timestamp(), hi.timestamp()],
            "skip": skip,
        }
        if len(keys) < limit:
            page["collect_keys"] = limit - len(keys)
        batch = await (fetch if index else first_fetch)(window, page)
        if not isinstance(batch, dict):
            return None
        total += batch["total"]
        skip = max(0, skip - batch["total"])
        keys.extend(batch["keys"])
    skipped = request["skip"] - skip
    rendered = (
        await fetch(command, {**request, "keys": keys}) if keys else {"entries": []}
    )
    if not rendered.get("byte_limited") and total <= skipped + len(keys):
        rendered["after"] = None
    return {
        **rendered,
        "matched": skipped + len(rendered["entries"]),
        "total": total,
        "skip": skip,
    }


async def fill_logbook_page(
    fetch: PageFetch,
    command: dict[str, Any],
    request: dict[str, Any],
    page: dict[str, Any],
    cursor: LogbookCursor | None,
) -> dict[str, Any]:
    """Resume after the last examined identity until the public page is full."""
    total = cursor.total if cursor else page["total"]
    matched = page["matched"]
    entries = page["entries"]
    while (
        len(entries) < request["limit"]
        and page.get("after")
        and not page.get("byte_limited")
        and not (
            cursor
            and not cursor.open_at_count
            and cursor.seen + matched >= cursor.total
        )
    ):
        page = await fetch(
            command,
            {
                **request,
                "after": page["after"],
                "skip": page["skip"],
                "limit": request["limit"] - len(entries),
                "used_bytes": page["used_bytes"],
            },
        )
        matched += page["matched"]
        entries.extend(page["entries"])
    if total is None and page.get("after") is None:
        total = matched
    return {**page, "entries": entries, "total": total, "matched": matched}
