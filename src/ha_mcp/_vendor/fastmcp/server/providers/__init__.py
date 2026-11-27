"""Providers for dynamic MCP components.

This module provides the `Provider` abstraction for providing tools,
resources, and prompts dynamically at runtime.

Example:
    ```python
    from fastmcp import FastMCP
    from fastmcp.server.providers import Provider
    from fastmcp.tools import Tool

    class DatabaseProvider(Provider):
        def __init__(self, db_url: str):
            self.db = Database(db_url)

        async def _list_tools(self) -> list[Tool]:
            rows = await self.db.fetch("SELECT * FROM tools")
            return [self._make_tool(row) for row in rows]

        async def _get_tool(self, name: str) -> Tool | None:
            row = await self.db.fetchone("SELECT * FROM tools WHERE name = ?", name)
            return self._make_tool(row) if row else None

    mcp = FastMCP("Server", providers=[DatabaseProvider(db_url)])
    ```
"""

from typing import TYPE_CHECKING

from ha_mcp._vendor.fastmcp.server.providers.aggregate import AggregateProvider
from ha_mcp._vendor.fastmcp.server.providers.base import Provider
from ha_mcp._vendor.fastmcp.server.providers.fastmcp_provider import FastMCPProvider
from ha_mcp._vendor.fastmcp.server.providers.filesystem import FileSystemProvider
from ha_mcp._vendor.fastmcp.server.providers.local_provider import LocalProvider

if TYPE_CHECKING:
    from ha_mcp._vendor.fastmcp.server.providers.skills import (
        ClaudeSkillsProvider as ClaudeSkillsProvider,
    )
    from ha_mcp._vendor.fastmcp.server.providers.skills import SkillProvider as SkillProvider
    from ha_mcp._vendor.fastmcp.server.providers.skills import (
        SkillsDirectoryProvider as SkillsDirectoryProvider,
    )
    from ha_mcp._vendor.fastmcp.server.providers.openapi import OpenAPIProvider as OpenAPIProvider
    from ha_mcp._vendor.fastmcp.server.providers.proxy import ProxyProvider as ProxyProvider

__all__ = [
    "AggregateProvider",
    "ClaudeSkillsProvider",
    "FastMCPProvider",
    "FileSystemProvider",
    "LocalProvider",
    "OpenAPIProvider",
    "Provider",
    "ProxyProvider",
    "SkillProvider",
    "SkillsDirectoryProvider",
]


def __getattr__(name: str) -> object:
    """Lazy import for providers to avoid circular imports."""
    if name == "ProxyProvider":
        from ha_mcp._vendor.fastmcp.server.providers.proxy import ProxyProvider

        return ProxyProvider
    if name == "OpenAPIProvider":
        from ha_mcp._vendor.fastmcp.server.providers.openapi import OpenAPIProvider

        return OpenAPIProvider
    if name in ("SkillProvider", "SkillsDirectoryProvider", "ClaudeSkillsProvider"):
        # Deprecated providers; constructing one warns with the replacement.
        from ha_mcp._vendor.fastmcp.server.providers import skills

        return getattr(skills, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
