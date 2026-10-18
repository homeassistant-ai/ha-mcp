"""Frozen value types shared by the client and its tool consumers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorLogPage:
    """One bounded window of the error log, plus whether older history exists.

    ``has_more`` cannot be derived by the caller from ``text``: a journald
    window that comes back full may or may not have anything behind it (see
    ``_journald_error_log_page``), and only the fetching route knows which.
    """

    text: str
    has_more: bool
