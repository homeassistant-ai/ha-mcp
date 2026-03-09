"""Categorized search transform for ha-mcp.

Extends FastMCP's BM25SearchTransform to provide a unified search tool
with separate call proxies for read, write, and delete operations.
Each proxy carries its own MCP annotations so clients can apply
appropriate permission policies (e.g., auto-approve reads, gate writes).

Tools are categorized by their existing MCP annotations:
- readOnlyHint=True → "read" category
- destructiveHint=True with remove/delete in name → "delete" category
- destructiveHint=True (other) → "write" category
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Annotated, Any

from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context
from fastmcp.server.transforms.search.bm25 import BM25SearchTransform
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations

from ..errors import ErrorCode, create_error_response

if TYPE_CHECKING:
    from fastmcp.server.transforms import GetToolNext
    from fastmcp.utilities.versions import VersionSpec

logger = logging.getLogger(__name__)

# Default HA tools to pin (always visible, bypass search transform)
DEFAULT_PINNED_TOOLS: tuple[str, ...] = (
    "ha_restart",
    "ha_reload_core",
    "ha_backup_create",
    "ha_backup_restore",
    "ha_get_overview",
    "ha_report_issue",
)

# Tool name patterns that indicate delete/remove operations
_DELETE_PATTERNS = ("_remove_", "_delete_")

# Proxy tool descriptions (shared between transform_tools and get_tool)
_READ_PROXY_DESC = (
    "Execute a read-only tool discovered via ha_search_tools. "
    "Safe — does not modify any data or state.\n"
    "Params: name (str) = tool name, arguments (dict) = tool parameters. "
    "These are separate top-level params, not nested.\n"
    "IMPORTANT: Call this tool SEQUENTIALLY, not in parallel with other proxy calls."
)
_WRITE_PROXY_DESC = (
    "Execute a write tool discovered via ha_search_tools. "
    "Creates or updates data. Use for any tool that modifies "
    "state but does not delete/remove resources.\n"
    "Params: name (str) = tool name, arguments (dict) = tool parameters. "
    "These are separate top-level params, not nested.\n"
    "IMPORTANT: Call this tool SEQUENTIALLY, not in parallel with other proxy calls."
)
_DELETE_PROXY_DESC = (
    "Execute a delete/remove tool discovered via ha_search_tools. "
    "Permanently removes data. Use for tools that delete or "
    "remove resources (areas, automations, devices, etc.).\n"
    "Params: name (str) = tool name, arguments (dict) = tool parameters. "
    "These are separate top-level params, not nested.\n"
    "IMPORTANT: Call this tool SEQUENTIALLY, not in parallel with other proxy calls."
)


def _categorize_tool(tool: Tool) -> str:
    """Categorize a tool as read, write, or delete based on annotations and name."""
    annotations = tool.annotations
    if annotations and annotations.readOnlyHint:
        return "read"
    # A tool is 'delete' only if it's destructive AND its name suggests deletion
    if annotations and annotations.destructiveHint and any(
        pattern in tool.name for pattern in _DELETE_PATTERNS
    ):
        return "delete"
    return "write"


class CategorizedSearchTransform(BM25SearchTransform):
    """BM25 search with categorized call proxies.

    Replaces the single ``call_tool`` proxy from BaseSearchTransform with
    three category-specific proxies, each carrying appropriate MCP
    annotations for client-side permission handling.

    The unified ``ha_search_tools`` is inherited from BM25SearchTransform and
    searches across ALL tools regardless of category. Search results include
    each tool's full annotations so the LLM can determine which proxy to use.
    """

    def __init__(
        self,
        *,
        max_results: int = 10,
        always_visible: list[str] | None = None,
        search_tool_name: str = "ha_search_tools",
        search_tool_description: str | None = None,
        call_read_name: str = "ha_call_read_tool",
        call_write_name: str = "ha_call_write_tool",
        call_delete_name: str = "ha_call_delete_tool",
        **kwargs: Any,
    ) -> None:
        super().__init__(
            max_results=max_results,
            always_visible=always_visible,
            search_tool_name=search_tool_name,
            # Use a placeholder call_tool_name — we override transform_tools
            # so the default call_tool is never surfaced.
            call_tool_name="_unused_call_tool",
            **kwargs,
        )
        self._call_read_name = call_read_name
        self._call_write_name = call_write_name
        self._call_delete_name = call_delete_name
        self._search_tool_description = search_tool_description

        # Category caches built lazily from the tool catalog
        self._read_tools: set[str] = set()
        self._write_tools: set[str] = set()
        self._delete_tools: set[str] = set()
        self._cache_built = False

    async def _rebuild_category_cache(self, ctx: Any) -> None:
        """Rebuild the read/write/delete category sets from the catalog."""
        catalog = await self.get_tool_catalog(ctx)
        self._read_tools.clear()
        self._write_tools.clear()
        self._delete_tools.clear()
        for tool in catalog:
            category = _categorize_tool(tool)
            if category == "read":
                self._read_tools.add(tool.name)
            elif category == "delete":
                self._delete_tools.add(tool.name)
            else:
                self._write_tools.add(tool.name)
        self._cache_built = True

    def _make_categorized_proxy(
        self,
        proxy_name: str,
        category: str,
        annotations: ToolAnnotations,
        description: str,
    ) -> Tool:
        """Create a call proxy that validates tool category before execution."""
        transform = self

        async def categorized_call(
            name: Annotated[str, "The name of the tool to call"],
            arguments: Annotated[
                dict[str, Any] | None, "Arguments to pass to the tool"
            ] = None,
            ctx: Context = None,  # type: ignore[assignment]
        ) -> Any:
            # Lazily build category cache if not already populated
            if not transform._cache_built:
                await transform._rebuild_category_cache(ctx)

            # Determine which category set to check
            if category == "read":
                allowed = transform._read_tools
            elif category == "delete":
                allowed = transform._delete_tools
            else:
                allowed = transform._write_tools

            # Detect and unwrap double-wrapped arguments where the LLM
            # accidentally nested name/arguments inside the arguments param
            # e.g. ha_call_read_tool(name="ha_call_read_tool",
            #   arguments={"name": "actual_tool", "arguments": {...}})
            if (
                arguments
                and isinstance(arguments.get("name"), str)
                and "arguments" in arguments
                and name in (
                    transform._call_read_name,
                    transform._call_write_name,
                    transform._call_delete_name,
                )
            ):
                logger.warning(
                    "Detected double-wrapped proxy call for '%s' via %s — unwrapping",
                    arguments["name"],
                    name,
                )
                name = arguments["name"]
                arguments = arguments.get("arguments") or {}

            if name not in allowed:
                # Provide a helpful error with the correct proxy name
                actual_category = "unknown"
                correct_proxy = ""
                if name in transform._read_tools:
                    actual_category = "read"
                    correct_proxy = transform._call_read_name
                elif name in transform._write_tools:
                    actual_category = "write"
                    correct_proxy = transform._call_write_name
                elif name in transform._delete_tools:
                    actual_category = "delete"
                    correct_proxy = transform._call_delete_name
                else:
                    raise ToolError(json.dumps(create_error_response(
                        code=ErrorCode.RESOURCE_NOT_FOUND,
                        message=f"Tool '{name}' not found. Use ha_search_tools to discover available tools.",
                        context={"tool_name": name},
                    )))
                raise ToolError(json.dumps(create_error_response(
                    code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                    message=f"Tool '{name}' is a {actual_category} tool. Use {correct_proxy} instead of {proxy_name}.",
                    suggestions=[f"Use '{correct_proxy}' for {actual_category} operations."],
                    context={"tool_name": name, "proxy_used": proxy_name, "correct_proxy": correct_proxy},
                )))

            return await ctx.fastmcp.call_tool(name, arguments)

        return Tool.from_function(
            fn=categorized_call,
            name=proxy_name,
            description=description,
            annotations=annotations,
        )

    async def transform_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        """Replace tool listing with search + categorized call proxies."""
        pinned = [t for t in tools if t.name in (self._always_visible or [])]

        search_tool = self._make_search_tool()
        # Always set readOnlyHint and override description if provided
        search_tool = Tool.from_function(
            fn=search_tool.fn,
            name=search_tool.name,
            description=self._search_tool_description or search_tool.description,
            annotations=ToolAnnotations(readOnlyHint=True),
        )

        call_read = self._make_categorized_proxy(
            proxy_name=self._call_read_name,
            category="read",
            annotations=ToolAnnotations(readOnlyHint=True),
            description=_READ_PROXY_DESC,
        )

        call_write = self._make_categorized_proxy(
            proxy_name=self._call_write_name,
            category="write",
            annotations=ToolAnnotations(destructiveHint=True),
            description=_WRITE_PROXY_DESC,
        )

        call_delete = self._make_categorized_proxy(
            proxy_name=self._call_delete_name,
            category="delete",
            annotations=ToolAnnotations(destructiveHint=True),
            description=_DELETE_PROXY_DESC,
        )

        return [*pinned, search_tool, call_read, call_write, call_delete]

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        """Resolve tool by name, including categorized proxy tools.

        The parent only handles _search_tool_name and _call_tool_name (unused).
        We must also intercept our three categorized proxy names so they can
        be found when the LLM calls them.
        """
        if name == self._call_read_name:
            return self._make_categorized_proxy(
                self._call_read_name, "read",
                ToolAnnotations(readOnlyHint=True),
                _READ_PROXY_DESC,
            )
        if name == self._call_write_name:
            return self._make_categorized_proxy(
                self._call_write_name, "write",
                ToolAnnotations(destructiveHint=True),
                _WRITE_PROXY_DESC,
            )
        if name == self._call_delete_name:
            return self._make_categorized_proxy(
                self._call_delete_name, "delete",
                ToolAnnotations(destructiveHint=True),
                _DELETE_PROXY_DESC,
            )
        return await super().get_tool(name, call_next, version=version)
