"""Frozen value types shared by the client and its tool consumers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorLogPage:
    """One bounded window of the error log, plus whether older history exists.

    ``has_more`` cannot be derived by the caller from ``text``: a journald
    window that comes back full may or may not have anything behind it, and
    only the fetching route knows which.

    ``first_cursor`` is Supervisor's ``X-First-Cursor`` for the block -- the
    oldest entry this window served -- and is ``None`` on installs that read a
    whole log file. The tool layer turns it into the ``next_offset`` token, so
    the cursor travels as data instead of being re-derived from the text.
    """

    text: str
    has_more: bool
    first_cursor: str | None = None
