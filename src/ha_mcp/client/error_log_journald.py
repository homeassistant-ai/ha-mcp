"""Journald window paging for the raw error log (Supervisor-backed installs).

Supervisor answers ``?lines=N`` with the N newest journald entries, and an
``offset`` counts back from the newest one. That anchor moves whenever Home
Assistant logs another line, so a caller paging on the number can re-read or
skip entries between two calls. Supervisor also returns ``X-First-Cursor``,
which identifies the first (oldest) entry of the block it served, and accepts
``Range: entries=<cursor>:<skip>:<count>``; HA's own log viewer pages on that
cursor for exactly this reason.

This module turns that into an opaque token: the first page is fetched the
existing way and carries ``next_cursor`` when more history exists, and a page
requested with a token is fetched relative to the token's cursor, so lines
logged in between cannot shift it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .models import ErrorLogPage

logger = logging.getLogger(__name__)

CURSOR_TOKEN_PREFIX = "cursor:"

# One extra entry is requested for a cursor page: the block ends at the
# token's cursor, which the caller already saw, so it is dropped.
_CURSOR_OVERLAP = 1


@dataclass(frozen=True)
class JournaldPage:
    """One journald block plus the cursor of its oldest entry."""

    text: str
    first_cursor: str | None


JournaldFetch = Callable[[str], Awaitable[JournaldPage]]


def is_cursor_token(value: object) -> bool:
    """True when ``value`` is a paging token produced by this module."""
    return (
        isinstance(value, str)
        and value.startswith(CURSOR_TOKEN_PREFIX)
        and bool(value[len(CURSOR_TOKEN_PREFIX) :])
    )


def cursor_token(cursor: str) -> str:
    """Build the opaque ``next_offset`` value for ``cursor``."""
    return f"{CURSOR_TOKEN_PREFIX}{cursor}"


def token_cursor(token: str) -> str:
    """The cursor carried by ``token``; raises ``ValueError`` when malformed."""
    cursor = token[len(CURSOR_TOKEN_PREFIX) :]
    if not cursor:
        raise ValueError("cursor token carries no cursor")
    return cursor


def range_header(lines: int, offset: int = 0) -> str:
    """The ``Range`` expression for the ``offset``-th window back from newest."""
    return f"entries=:{-(offset + lines - 1)}:{lines}"


def cursor_range_header(cursor: str, lines: int) -> str:
    """The expression returning ``lines`` entries ending at ``cursor``.

    The block is oldest-first and ends on the anchor entry, so callers drop
    that last entry to get the entries strictly before the cursor.
    """
    return f"entries={cursor}:{-(lines)}:{lines}"


def container_page(text: str, lines: int, offset: int) -> ErrorLogPage:
    """Window the whole-file ``/api/error_log`` body client-side.

    Paging is exact here: the whole file is in hand, so ``has_more`` is simply
    whether anything precedes the window.
    """
    all_lines = text.splitlines()
    end = len(all_lines) - offset
    if end <= 0:
        return ErrorLogPage(text="", has_more=False)
    start = max(end - lines, 0)
    return ErrorLogPage(text="\n".join(all_lines[start:end]), has_more=start > 0)


def first_cursor(response: object) -> str | None:
    """Supervisor's ``X-First-Cursor`` from ``response``, when it carries one."""
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    value = headers.get("X-First-Cursor")
    return value or None


async def journald_page(
    lines: int,
    offset: int | str,
    fetch: JournaldFetch,
) -> ErrorLogPage:
    """One journald window, anchored on the newest entry or on a cursor token."""
    if is_cursor_token(offset):
        if not isinstance(offset, str):  # pragma: no cover - narrows the type
            raise ValueError("cursor token must be a string")
        return await _cursor_page(lines, token_cursor(offset), fetch)
    if isinstance(offset, str):
        raise ValueError("offset must be a non-negative integer or a cursor token")
    return await _newest_page(lines, offset, fetch)


async def _newest_page(lines: int, offset: int, fetch: JournaldFetch) -> ErrorLogPage:
    page = await fetch(range_header(lines, offset))
    text_lines = page.text.splitlines()
    if len(text_lines) < lines:
        # Nothing clamps a window smaller than asked for, so a short window is
        # the end of the journal.
        return ErrorLogPage(text=page.text, has_more=False)
    has_more = await _has_older_than(page.first_cursor, fetch)
    return ErrorLogPage(
        text=page.text,
        has_more=has_more,
        next_cursor=cursor_token(page.first_cursor) if page.first_cursor else None,
    )


async def _cursor_page(lines: int, cursor: str, fetch: JournaldFetch) -> ErrorLogPage:
    page = await fetch(cursor_range_header(cursor, lines + _CURSOR_OVERLAP))
    entries = page.text.splitlines()
    older = entries[:-_CURSOR_OVERLAP] if len(entries) > _CURSOR_OVERLAP else []
    if not older:
        return ErrorLogPage(text="", has_more=False)
    has_more = await _has_older_than(page.first_cursor, fetch)
    return ErrorLogPage(
        text="\n".join(older),
        has_more=has_more,
        next_cursor=cursor_token(page.first_cursor) if page.first_cursor else None,
    )


async def _has_older_than(cursor: str | None, fetch: JournaldFetch) -> bool:
    """True when an entry precedes ``cursor``.

    Asking for the two entries ending at the cursor returns just the cursor's
    own entry when it is the oldest one in the journal — Supervisor clamps
    negative skips there instead of answering with an empty block.
    """
    if not cursor:
        return False
    probe = await fetch(cursor_range_header(cursor, 2))
    return len(probe.text.splitlines()) > 1
