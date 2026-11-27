"""Frozen value types shared by the client and its tool consumers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorLogPage:
    """One bounded window of the error log, plus whether older history exists.

    ``has_more`` cannot be derived by the caller from ``text``: a journald
    window that comes back full may or may not have anything behind it, and
    only the fetching route knows which.

    ``first_cursor`` names the oldest line this window served: Supervisor's
    ``X-First-Cursor`` for a journal block, or the component's position in the
    log file. It is ``None`` when the whole file was read without the
    component. The tool layer turns it into the ``next_offset`` token, so the
    cursor travels as data instead of being re-derived from the text.
    """

    text: str
    has_more: bool
    first_cursor: str | None = None
