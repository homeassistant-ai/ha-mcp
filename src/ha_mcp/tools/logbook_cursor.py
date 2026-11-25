"""Continuation for a component-backed whole-instance Recorder page."""

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Literal

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
