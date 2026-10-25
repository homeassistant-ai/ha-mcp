"""Journald window paging for the raw error log (Supervisor-backed installs).

Supervisor answers ``?lines=N`` with the N newest journald entries, and an
``offset`` counts back from the newest one. That anchor moves whenever Home
Assistant logs another line, so a caller paging on the number can re-read or
skip entries between two calls. Supervisor also returns ``X-First-Cursor``,
which identifies the first (oldest) entry of the block it served, and accepts
``Range: entries=<cursor>:<skip>:<count>``; HA's own log viewer pages on that
cursor for exactly this reason.

This module turns that into an opaque token carrying both the cursor and the
number of entries a page consumed from it: ``cursor:<X-First-Cursor>:<skip>``.
A page requested with a token is fetched entirely behind that resume point, so
lines logged in between cannot shift it and a page that returned only part of
its fetched window still resumes exactly. ``journald_page`` never computes
the skip itself -- only the tool layer knows which records a page returned --
so the token is built there and this module only applies it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .models import ErrorLogPage

logger = logging.getLogger(__name__)

CURSOR_TOKEN_PREFIX = "cursor:"


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


def cursor_token(cursor: str, skip: int) -> str:
    """Build the opaque ``next_offset`` value resuming ``skip`` entries in.

    ``skip`` counts entries forward from ``cursor`` (the oldest entry of the
    page's fetched window) to the resume point: the first entry the page did
    not return. Zero means the page used its whole window.
    """
    if skip < 0:
        raise ValueError(f"skip must not be negative, got {skip}")
    return f"{CURSOR_TOKEN_PREFIX}{cursor}:{skip}"


def parse_cursor_token(token: str) -> tuple[str, int]:
    """Split ``token`` into its cursor and skip; raises ``ValueError`` if malformed.

    The cursor itself never contains ``:`` (systemd cursors separate their
    fields with ``;``), but the skip is still split off from the right so a
    future cursor format cannot be mistaken for it.
    """
    if not token.startswith(CURSOR_TOKEN_PREFIX):
        raise ValueError("not a cursor token")
    cursor, separator, skip = token[len(CURSOR_TOKEN_PREFIX) :].rpartition(":")
    if not separator or not cursor or not skip.isdigit():
        raise ValueError(
            "cursor token must read 'cursor:<cursor>:<entries>' as returned in "
            "'next_offset'"
        )
    return cursor, int(skip)


def token_cursor(token: str) -> str:
    """The cursor carried by ``token``; raises ``ValueError`` when malformed."""
    return parse_cursor_token(token)[0]


def token_skip(token: str) -> int:
    """The skip carried by ``token``; raises ``ValueError`` when malformed."""
    return parse_cursor_token(token)[1]


def range_header(lines: int, offset: int = 0) -> str:
    """The ``Range`` expression for the ``offset``-th window back from newest."""
    return f"entries=:{-(offset + lines - 1)}:{lines}"


def cursor_range_header(cursor: str, skip: int, lines: int) -> str:
    """The expression returning ``lines`` entries strictly behind ``cursor:skip``.

    Gatewayd reads ``entries=<cursor>:<num_skip>:<num_entries>`` forward from
    ``cursor + num_skip``, so ending the block just behind the resume point
    (``cursor + skip``) means starting ``lines`` entries earlier. That makes a
    cursor page the exact unfetched remainder: nothing the caller already saw
    is repeated and nothing between pages is dropped.
    """
    return f"entries={cursor}:{skip - lines}:{lines}"


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
    return ErrorLogPage(
        text="\n".join(all_lines[start:end]),
        has_more=start > 0,
        first_cursor=None,
    )


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
        cursor, skip = parse_cursor_token(offset)
        return await _cursor_page(lines, cursor, skip, fetch)
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
        first_cursor=page.first_cursor,
    )


async def _cursor_page(
    lines: int, cursor: str, skip: int, fetch: JournaldFetch
) -> ErrorLogPage:
    page = await fetch(cursor_range_header(cursor, skip, lines))
    entries = page.text.splitlines()
    if len(entries) < lines:
        # A short block is the journal's start or a clamped anchor: no more.
        return ErrorLogPage(
            text=page.text, has_more=False, first_cursor=page.first_cursor
        )
    has_more = await _has_older_than(page.first_cursor, fetch)
    return ErrorLogPage(
        text=page.text,
        has_more=has_more,
        first_cursor=page.first_cursor,
    )


async def _has_older_than(cursor: str | None, fetch: JournaldFetch) -> bool:
    """True when an entry precedes ``cursor``.

    Gatewayd clamps a skip that runs past the journal's start to its head
    block instead of answering with an empty one, so the block's length says
    nothing here. Its first cursor does: only a clamped probe starts on the
    anchor entry itself. (The HA frontend uses the same signal to detect the
    end of history.)
    """
    if not cursor:
        return False
    probe = await fetch(cursor_range_header(cursor, 0, 2))
    if not probe.text.splitlines():
        return False
    return probe.first_cursor != cursor
