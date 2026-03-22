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

import asyncio
import hashlib
import json
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context
from fastmcp.server.transforms import Transform
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
    "ha_search_entities",
    "ha_config_get_automation",
    "ha_config_set_automation",
)

# Tool name patterns that indicate delete/remove operations
_DELETE_PATTERNS = ("_remove_", "_delete_")


class SearchKeywordsTransform(Transform):
    """Adjust BM25 search keywords in tool descriptions.

    Supports two modes per tool:
    - **keywords** (append): Extra keywords appended after the original
      description so BM25 ranks the tool higher for common queries.
    - **overrides** (replace): Completely replaces the description with
      a narrower one so BM25 ranks the tool *lower* for broad queries.

    The original description is preserved unless an override is applied.
    Only active when added to the transform pipeline (i.e., behind
    the ``enable_tool_search`` toggle).
    """

    def __init__(
        self,
        keywords: dict[str, str] | None = None,
        overrides: dict[str, str] | None = None,
    ) -> None:
        """Initialize with optional keyword boosts and description overrides."""
        self._keywords = keywords or {}
        self._overrides = overrides or {}

    def _enrich(self, tool: Tool) -> Tool:
        # Overrides take priority — replace the entire description
        override = self._overrides.get(tool.name)
        if override is not None:
            return tool.model_copy(update={"description": override})
        # Otherwise append keywords if present
        keywords = self._keywords.get(tool.name)
        if not keywords:
            return tool
        enriched = f"{tool.description}\n\n{keywords}" if tool.description else keywords
        return tool.model_copy(update={"description": enriched})

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        return [self._enrich(t) for t in tools]

    async def get_tool(
        self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None
    ) -> Tool | None:
        tool = await call_next(name, version=version)
        return self._enrich(tool) if tool else None

# Proxy description suffix (shared across all proxies)
_PROXY_PARAMS_SUFFIX = (
    "Params: name (str) = tool name, arguments (dict) = tool parameters. "
    "These are separate top-level params, not nested.\n"
    "IMPORTANT: Call this tool SEQUENTIALLY, not in parallel with other proxy calls."
)


def _build_proxy_descriptions(search_tool_name: str) -> dict[str, str]:
    """Build proxy descriptions that reference the configured search tool name."""
    return {
        "read": (
            f"Execute a read-only tool discovered via {search_tool_name}. "
            f"Safe — does not modify any data or state.\n"
            f"{_PROXY_PARAMS_SUFFIX}"
        ),
        "write": (
            f"Execute a write tool discovered via {search_tool_name}. "
            f"Creates or updates data. Use for any tool that modifies "
            f"state but does not delete/remove resources.\n"
            f"{_PROXY_PARAMS_SUFFIX}"
        ),
        "delete": (
            f"Execute a delete/remove tool discovered via {search_tool_name}. "
            f"Permanently removes data. Use for tools that delete or "
            f"remove resources (areas, automations, devices, etc.).\n"
            f"{_PROXY_PARAMS_SUFFIX}"
        ),
    }


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
        max_results: int = 5,
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
            # Placeholder call_tool_name — we override transform_tools with
            # categorized proxies so the base class's single call proxy is
            # never surfaced to clients.
            call_tool_name="_base_call_proxy",
            **kwargs,
        )
        self._call_read_name = call_read_name
        self._call_write_name = call_write_name
        self._call_delete_name = call_delete_name
        self._search_tool_description = search_tool_description
        self._proxy_descs = _build_proxy_descriptions(search_tool_name)

        # Category caches rebuilt when the catalog hash changes,
        # matching BM25SearchTransform's staleness detection pattern.
        self._read_tools: set[str] = set()
        self._write_tools: set[str] = set()
        self._delete_tools: set[str] = set()
        self._last_catalog_hash: str = ""
        self._cache_lock = asyncio.Lock()

    @staticmethod
    def _catalog_hash(tools: Sequence[Tool]) -> str:
        """Hash tool names + categories for staleness detection."""
        key = "|".join(
            sorted(f"{t.name}:{_categorize_tool(t)}" for t in tools)
        )
        return hashlib.sha256(key.encode()).hexdigest()

    async def _rebuild_category_cache(self, ctx: Any) -> None:
        """Rebuild the read/write/delete category sets if catalog changed."""
        catalog = await self.get_tool_catalog(ctx)
        current_hash = self._catalog_hash(catalog)
        if current_hash == self._last_catalog_hash:
            return
        async with self._cache_lock:
            # Double-check after acquiring lock
            if current_hash == self._last_catalog_hash:
                return
            read: set[str] = set()
            write: set[str] = set()
            delete: set[str] = set()
            for tool in catalog:
                cat = _categorize_tool(tool)
                if cat == "read":
                    read.add(tool.name)
                elif cat == "delete":
                    delete.add(tool.name)
                else:
                    write.add(tool.name)
            self._read_tools = read
            self._write_tools = write
            self._delete_tools = delete
            self._last_catalog_hash = current_hash

    async def _render_results(self, tools: Sequence[Tool]) -> list[dict[str, Any]]:
        """Serialize search results with ``execute_via`` hints."""
        proxy_map = {
            "read": self._call_read_name,
            "write": self._call_write_name,
            "delete": self._call_delete_name,
        }
        results = []
        for tool in tools:
            data = tool.to_mcp_tool().model_dump(mode="json", exclude_none=True)
            proxy = proxy_map[_categorize_tool(tool)]
            data["execute_via"] = (
                f'client.{proxy}(name="{tool.name}", arguments={{...}}) '
                f'or {proxy}(name="{tool.name}", arguments={{...}})'
            )
            results.append(data)
        return results

    def _make_categorized_proxy(
        self,
        proxy_name: str,
        category: Literal["read", "write", "delete"],
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
            # Rebuild category cache if catalog has changed
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
            all_known = (
                transform._read_tools | transform._write_tools | transform._delete_tools
            )
            if (
                arguments
                and isinstance(arguments.get("name"), str)
                and "arguments" in arguments
                and name in (
                    transform._call_read_name,
                    transform._call_write_name,
                    transform._call_delete_name,
                )
                and arguments["name"] in all_known
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
        search_tool = search_tool.model_copy(update={
            "description": self._search_tool_description or search_tool.description,
            "annotations": ToolAnnotations(readOnlyHint=True),
        })

        call_read = self._make_categorized_proxy(
            proxy_name=self._call_read_name,
            category="read",
            annotations=ToolAnnotations(readOnlyHint=True),
            description=self._proxy_descs["read"],
        )

        call_write = self._make_categorized_proxy(
            proxy_name=self._call_write_name,
            category="write",
            annotations=ToolAnnotations(destructiveHint=True),
            description=self._proxy_descs["write"],
        )

        call_delete = self._make_categorized_proxy(
            proxy_name=self._call_delete_name,
            category="delete",
            annotations=ToolAnnotations(destructiveHint=True),
            description=self._proxy_descs["delete"],
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
                self._proxy_descs["read"],
            )
        if name == self._call_write_name:
            return self._make_categorized_proxy(
                self._call_write_name, "write",
                ToolAnnotations(destructiveHint=True),
                self._proxy_descs["write"],
            )
        if name == self._call_delete_name:
            return self._make_categorized_proxy(
                self._call_delete_name, "delete",
                ToolAnnotations(destructiveHint=True),
                self._proxy_descs["delete"],
            )
        return await super().get_tool(name, call_next, version=version)
