"""Unit tests for CategorizedSearchTransform.

Tests the categorization logic, transform_tools output, get_tool resolution,
proxy category validation, dispatch execution, and SearchKeywordsTransform.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp._vendor.fastmcp.tools import Tool
from ha_mcp._vendor.mcp.types import ToolAnnotations
from ha_mcp.config import get_global_settings

from ._search_mode_connection import search_connection as search_connection


@pytest.fixture
def read_only_on(monkeypatch):
    monkeypatch.setattr(get_global_settings(), "read_only_mode", True)


def _make_tool(
    name: str,
    *,
    read_only: bool = False,
    destructive: bool = False,
    idempotent: bool = False,
    description: str = "",
) -> Tool:
    """Create a minimal Tool for testing."""

    async def noop() -> str:
        return "ok"

    annotations = ToolAnnotations(
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
    )
    return Tool.from_function(
        fn=noop, name=name, description=description, annotations=annotations
    )


# ---------------------------------------------------------------------------
# _categorize_tool
# ---------------------------------------------------------------------------


def set_catalog(transform, tools):
    """Supply this transform's MCP catalog; it builds its own dispatch cache."""
    transform.get_tool_catalog = AsyncMock(return_value=tools)


async def proxy_function(transform, category):
    proxy = await transform.get_tool(
        f"ha_call_{category}_tool", AsyncMock(return_value=None)
    )
    assert proxy is not None
    return proxy.fn


async def render_search_results(transform, tools):
    """Read compact hits from one real MCP search over the complete catalog."""
    mcp = FastMCP("categorized-search")
    for tool in tools:
        mcp.add_tool(tool)
    mcp.add_transform(transform)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "ha_search_tools", {"query": " ".join(tool.name for tool in tools)}
        )
    return list(result.data)


def _make_ctx(call_tool_return: Any = "tool_result"):
    """Create a mock Context with fastmcp.call_tool."""
    ctx = MagicMock()
    ctx.fastmcp.call_tool = AsyncMock(return_value=call_tool_return)
    return ctx
