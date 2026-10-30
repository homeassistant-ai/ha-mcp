"""Shared stubs for the raw error-log unit tests."""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from ha_mcp.client.error_log_journald import (
    JournaldFetch,
    JournaldPage,
    journald_page,
)
from ha_mcp.client.rest_client import ErrorLogPage
from ha_mcp.tools.tools_logs import register_logs_tools

from ._error_log_structured_fixtures import _SAMPLE_LOG


def make_client(log_text: str = _SAMPLE_LOG, *, has_more: bool = False) -> MagicMock:
    """Client stub whose ``get_error_log`` answers with one page.

    ``has_more`` is the client's verdict, not a property of ``log_text``: on
    journald only the client can establish it (see ``_journald_error_log_page``),
    so the tool layer takes it as given.
    """
    client = MagicMock()
    client.get_error_log = AsyncMock(
        return_value=ErrorLogPage(text=log_text, has_more=has_more)
    )
    return client


def register_and_collect(client: Any) -> dict[str, Any]:
    """Register the logs tools on a tiny collector and return them by name."""
    collected: dict[str, Any] = {}

    def _tool(**_kwargs: Any) -> Any:
        def _wrap(fn: Any) -> Any:
            collected[fn.__name__] = fn
            return fn

        return _wrap

    mcp = SimpleNamespace(tool=_tool)
    register_logs_tools(mcp, client)
    return collected


def numbered_log(count: int) -> str:
    """`count` parseable log lines — enough to fill an exact fetch window."""
    return "".join(
        f"2026-05-27 10:00:00.000 ERROR (MainThread) [a.b.c] issue {i}\n"
        for i in range(count)
    )


def journald_fetch(entries: list[str]) -> JournaldFetch:
    """Serve gatewayd ``Range`` requests from ``entries`` (oldest first).

    Mirrors the semantics measured against Supervisor: ``entries=<cursor>:
    <skip>:<count>`` reads ``count`` entries forward from ``cursor + skip``, an
    empty cursor anchors on the newest entry, and a start before the journal's
    head clamps there and still reads forward. That last part is what lets a
    clamped block overrun its resume point, so tests can pin the trimming.
    Cursors are ``e<index>``.
    """
    pattern = re.compile(r"^entries=(?P<cursor>[^:]*):(?P<skip>-?\d+):(?P<count>\d+)$")
    cursors = [f"e{index}" for index in range(len(entries))]
    positions = {cursor: index for index, cursor in enumerate(cursors)}

    async def _fetch(range_header: str) -> JournaldPage:
        match = pattern.match(range_header)
        assert match, f"unexpected Range header: {range_header}"
        skip = int(match.group("skip"))
        count = int(match.group("count"))
        if match.group("cursor"):
            start = positions[match.group("cursor")] + skip
        else:
            start = len(entries) - 1 + skip
        start = max(0, start)
        block = entries[start : start + count]
        return JournaldPage("\n".join(block), cursors[start] if block else None)

    return _fetch


def journald_page_client(entries: list[str]) -> AsyncMock:
    """``get_error_log`` stub resolving cursor tokens like Supervisor does."""
    fetch = journald_fetch(entries)

    async def _page(lines: int, offset: int | str = 0) -> ErrorLogPage:
        return await journald_page(lines, offset, fetch)

    client = AsyncMock()
    client.get_error_log = _page
    return client
