"""Shared stubs for the raw error-log unit tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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
