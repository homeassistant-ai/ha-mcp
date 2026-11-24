"""Continuation for a component-backed whole-instance Recorder page."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, FiniteFloat

LOGBOOK_CURSOR_PREFIX = "logbook:"


class LogbookCursor(BaseModel):
    end: datetime
    order: Literal["newest", "oldest"]
    search: str | None
    after: tuple[FiniteFloat, Literal[0, 1], int]
    seen: int = Field(ge=0)
    skip: int = Field(ge=0)

    def token(self) -> str:
        return LOGBOOK_CURSOR_PREFIX + self.model_dump_json()

    @classmethod
    def parse(cls, value: str) -> "LogbookCursor":
        return cls.model_validate_json(value.removeprefix(LOGBOOK_CURSOR_PREFIX))
