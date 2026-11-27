"""Continuation for a component-backed whole-instance Recorder page."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, FiniteFloat

LOGBOOK_CURSOR_PREFIX = "logbook:"


class LogbookCursor(BaseModel):
    hours_back: int = Field(default=1, ge=1)
    total: int | None = Field(ge=0)
    lower_bound: int = Field(default=0, ge=0)
    scope: dict[str, Any] = Field(default_factory=dict)
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


type PageFetch = Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]

# HA-MCP policy: cap recorder pages per tool call, including probes for a total.
MAX_PAGE_READS = 16


async def fill_logbook_page(
    fetch: PageFetch,
    command: dict[str, Any],
    request: dict[str, Any],
    page: dict[str, Any],
    cursor: LogbookCursor | None,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    before = cursor.seen if cursor else 0
    seen = before
    total = cursor.total if cursor else None
    lower = cursor.lower_bound if cursor else 0
    continuation = None
    for index in range(MAX_PAGE_READS):
        if total is None and page["total"] is not None:
            total = seen + page["total"]
        seen += page["matched"]
        lower = max(lower, seen)
        if continuation is None:
            entries.extend(page["entries"])
            if (
                len(entries) == request["limit"]
                or page.get("byte_limited")
                or not page.get("after")
                or index == MAX_PAGE_READS - 1
            ):
                continuation = {**page, "matched": seen - before}
        if (
            not page.get("after")
            or (continuation is not None and total is not None)
            or index == MAX_PAGE_READS - 1
        ):
            break
        counting = continuation is not None
        page = await fetch(
            command,
            {
                **request,
                "after": page["after"],
                "skip": page["skip"],
                "limit": 500 if counting else request["limit"] - len(entries),
                "used_bytes": 0 if counting else page["used_bytes"],
            },
        )
    assert continuation is not None
    return {**continuation, "entries": entries, "total": total, "lower_bound": lower}
