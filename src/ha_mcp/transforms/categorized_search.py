"""Categorized search transform for ha-mcp.

Extends FastMCP's BM25SearchTransform to provide a unified search tool
with separate call proxies for read, write, and delete operations.
Each proxy carries its own MCP annotations so clients can apply
appropriate permission policies (e.g., auto-approve reads, gate writes).

Tools are categorized by their existing MCP annotations:
- readOnlyHint=True → "read" category
- destructiveHint=True with remove/delete in name → "delete" category
- destructiveHint=True (other) → "write" category

A ``manage`` tool combines several operations behind one name, so it is
reachable from every proxy — read-approved calls only on the read proxy,
the whole tool on the write and delete proxies (see ``_admits``) — and
search results point each kind of action at its own proxy. In Read Only
Mode only the read proxy is listed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Annotated, Any, Literal, NoReturn

from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context
from fastmcp.server.transforms import Transform
from fastmcp.server.transforms.search.bm25 import BM25SearchTransform
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations

from ..errors import ErrorCode, create_error_response
from ..renamed_tools import adapt_retired_arguments, current_tool_name

if TYPE_CHECKING:
    from fastmcp.server.transforms import GetToolNext
    from fastmcp.utilities.versions import VersionSpec

logger = logging.getLogger(__name__)

# Default HA tools to pin (always visible, bypass search transform).
#
# Most of these are defaults only — users can unpin them via the Tools
# tab in the settings UI, which sets the tool's state to ``"enabled"`` in
# ``tool_config.json``. Server-side, the effective pinned set is computed
# as ``DEFAULT_PINNED_TOOLS`` minus any tool whose saved state is
# ``"enabled"``, plus any user-pinned tools. Tools with no entry in
# ``tool_config.json`` stay pinned by default.
#
# EXCEPTION: tools that are also in settings_ui._tools_meta
# .MANDATORY_TOOLS (ha_search, ha_get_overview, ha_report_issue,
# ha_manage_backup) cannot be unpinned — the settings UI locks their pin
# toggle, so they are always pinned as well as always enabled. This is
# intentional (#2058): mandatory tools must stay discoverable in the
# advertised catalog, not merely callable through the ha_call_*_tool
# proxies, because hiding them can break the workflows they anchor (e.g.
# the backup safety net before config writes). ha_get_skill_guide gets
# the same pin lock while strict best-practices mode is on
# (BPS_MANDATORY_TOOLS, #1886).
#
# Removed in #966 (operational recovery actions, low frequency, low value
# in the default LLM tool surface — still discoverable via tool search):
#   - ``ha_restart``
#   - ``ha_reload_core``
#
# ``ha_config_set_yaml`` and ``ha_manage_custom_tool`` were previously
# pinned here (the latter conditionally in server.py when code mode was
# enabled) so users could gate them via per-tool MCP permission prompts
# even when toolsearch hid the rest of the catalog. The tool security
# policies middleware shipped in #966 now gates those tools at call time
# regardless of catalog visibility, so they no longer need to be pinned
# just to be reachable for gating — keeping them behind the search proxy
# reduces the LLM's tool surface without sacrificing the safety check.
DEFAULT_PINNED_TOOLS: tuple[str, ...] = (
    "ha_manage_backup",
    "ha_get_overview",
    "ha_report_issue",
    "ha_search",
    "ha_config_get_automation",
    "ha_config_set_automation",
    # Skill guide must stay visible when tool search hides the catalog —
    # its description carries the bundled best-practices trigger
    # conditions that the LLM needs to see before writing config.
    "ha_get_skill_guide",
)

# Tool name patterns that indicate delete/remove operations
_DELETE_PATTERNS = ("_remove_", "_delete_")

# ``manage`` names one interface that intentionally combines several
# operations (.gemini/styleguide.md, Tool Naming Convention). Such a tool is
# categorised by its annotations like any other, but every call proxy can
# reach it — see ``_admits``.
_MANAGE_PATTERNS = ("_manage_",)

# Capability tier a tool falls into — shared by the call-proxy routing and
# the settings-UI capability badges. See ``categorize_capability``.
Capability = Literal["read", "write", "delete"]


class SearchKeywordsTransform(Transform):
    """Adjust BM25 search keywords in tool descriptions.

    Supports two modes per tool:
    - **keywords** (append): Extra keywords appended after the original
      description so BM25 ranks the tool higher for common queries.
    - **overrides** (replace): Completely replaces the description with
      a narrower one so BM25 ranks the tool *lower* for broad queries.

    The original description is preserved unless an override is applied.

    Added to the transform pipeline unconditionally (#940), so ``keywords``
    reach every client regardless of ``enable_tool_search``; only
    ``overrides`` are gated behind that toggle.
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
            f"{_PROXY_PARAMS_SUFFIX}\n"
            f'EXAMPLE: ha_call_read_tool(name="ha_get_history", arguments={{"entity_ids": "light.x", "start_time": "24h"}})'
        ),
        "write": (
            f"Execute a write tool discovered via {search_tool_name}. "
            f"Creates or updates data. Use for any tool that modifies "
            f"state but does not delete/remove resources.\n"
            f"{_PROXY_PARAMS_SUFFIX}\n"
            f'EXAMPLE: ha_call_write_tool(name="ha_set_area_or_floor", arguments={{"kind": "area", "name": "Kitchen"}})'
        ),
        "delete": (
            f"Execute a delete/remove tool discovered via {search_tool_name}. "
            f"Permanently removes data. Use for tools that delete or "
            f"remove resources (areas, automations, devices, etc.).\n"
            f"{_PROXY_PARAMS_SUFFIX}\n"
            f'EXAMPLE: ha_call_delete_tool(name="ha_remove_area_or_floor", arguments={{"kind": "area", "id": "old_area"}})'
        ),
    }


def categorize_capability(
    name: str, *, read_only: bool, destructive: bool
) -> Capability:
    """Categorize a tool as ``read``, ``write``, or ``delete``.

    Derived from the MCP annotations (``readOnlyHint``/``destructiveHint``):
    read-only tools are ``read``; destructive tools whose name matches
    ``_remove_``/``_delete_`` are ``delete``; everything else (including
    non-destructive, non-read-only tools) is ``write`` — ``write`` is the
    fallback bucket, not a subset of the destructive set. Shared by the
    categorized-search call proxies and the settings-UI capability badges so
    the two surfaces always agree on a tool's category.
    """
    if read_only:
        return "read"
    # A tool is 'delete' only if it's destructive AND its name suggests deletion
    if destructive and any(pattern in name for pattern in _DELETE_PATTERNS):
        return "delete"
    return "write"


def _is_manage_tool(name: str) -> bool:
    """Whether *name* follows the ``manage`` convention for a multi-operation tool."""
    return any(pattern in name for pattern in _MANAGE_PATTERNS)


def _is_read_call_on_write_tool(name: str, arguments: dict[str, Any] | None) -> bool:
    """Whether this call on a non-read-category tool is one of its read actions.

    A mixed read/write tool (``ha_manage_backup``, ``ha_manage_updates``,
    ``ha_manage_blueprints``, ...) is categorised ``write`` by its annotations
    and lives in that category set only. But refusing its list/get actions on
    the read proxy would strip real read surface from a client that only holds
    that proxy — for blueprints, folding the old read tool into the merged one
    would otherwise have removed listing entirely (#2329).

    Read-only mode already enumerates, per call, which invocations of such a
    tool are reads (``READ_ONLY_EXEMPT_TOOLS``), and the proxies reuse that
    verdict so the two surfaces cannot disagree about what counts as a read.
    The verdict fails closed: a tool without an exemption entry, or a missing
    or unknown action, is never a read.
    """
    from ..read_only import READ_ONLY_EXEMPT_TOOLS

    exemption = READ_ONLY_EXEMPT_TOOLS.get(name)
    return exemption is not None and exemption.blocked_write(arguments or {}) is None


def _has_read_actions(name: str) -> bool:
    """Whether a write-category tool has read actions the read proxy can approve."""
    from ..read_only import READ_ONLY_EXEMPT_TOOLS

    return name in READ_ONLY_EXEMPT_TOOLS


def _admits(
    transform: CategorizedSearchTransform,
    category: Capability,
    name: str,
    arguments: dict[str, Any] | None,
) -> bool:
    """Whether the *category* proxy may dispatch this call.

    Membership in the category set is the rule. A ``manage`` tool (#2358) is
    the exception: it is reachable from every proxy. The read proxy is the
    one hard boundary — it carries ``readOnlyHint`` — so it admits only a
    call the read-only predicate approves. The write and delete proxies both
    carry ``destructiveHint`` and run the whole tool: its delete actions are
    not statically enumerable (``ha_manage_radio`` and ``ha_manage_updates``
    take a free-form ``action``), so neither proxy pretends to carve them out.
    """
    if category == "read":
        return name in transform._read_tools or (
            name in transform._write_tools
            and _is_read_call_on_write_tool(name, arguments)
        )
    if category == "write":
        return name in transform._write_tools
    return name in transform._delete_tools or (
        name in transform._write_tools and _is_manage_tool(name)
    )


def _execute_via(proxy: str, tool_name: str) -> str:
    """Render the call form a search result advertises for *tool_name*."""
    return (
        f'client.{proxy}(name="{tool_name}", arguments={{...}}) '
        f'or {proxy}(name="{tool_name}", arguments={{...}})'
    )


def _read_only_mode() -> bool:
    """Whether Read Only Mode is on — consulted per request, like its filter."""
    from ..config import get_global_settings

    return bool(get_global_settings().read_only_mode)


def _advertised_routes(name: str, category: Capability) -> list[Capability]:
    """Proxies a search result points at for *name*, in listing order.

    A manage tool lists every proxy it is reachable through (see
    ``_admits``); the read route exists only when the read-only predicate can
    approve calls to it. In Read Only Mode the destructive proxies are not
    listed, so only the read route remains. The ``["write", "delete"]`` case
    cannot occur in that mode: ``ReadOnlyToolsTransform`` drops every
    non-exempt write tool from the catalog before the search index is built,
    and every exempt tool has read actions.
    """
    if category != "write" or not _is_manage_tool(name):
        return [category]
    if _has_read_actions(name):
        return ["read"] if _read_only_mode() else ["read", "write", "delete"]
    return ["write", "delete"]


def _categorize_tool(tool: Tool) -> Capability:
    """Categorize a Tool as read, write, or delete based on annotations and name."""
    annotations = tool.annotations
    return categorize_capability(
        tool.name,
        read_only=bool(annotations and annotations.readOnlyHint),
        destructive=bool(annotations and annotations.destructiveHint),
    )


def _raise_non_object_arguments(value: Any, proxy_name: str, name: str) -> NoReturn:
    """Refuse a proxy ``arguments`` payload that is not an object."""
    raise ToolError(
        json.dumps(
            create_error_response(
                code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                message=(
                    f"'arguments' must be a JSON object (got {type(value).__name__})."
                ),
                suggestions=[
                    "Pass 'arguments' as an object (dict), not a list or scalar.",
                ],
                context={"proxy_used": proxy_name, "tool_name": name},
            )
        )
    )


def _coerce_proxy_arguments(
    arguments: dict[str, Any] | str | None,
    proxy_name: str,
    name: str,
) -> dict[str, Any] | None:
    """Coerce a proxy call's ``arguments`` to a dict, tolerating a JSON string.

    Small models sometimes serialize ``arguments`` before sending. Parse once up
    front so downstream logic can assume a dict (or None).
    """
    if not isinstance(arguments, str):
        return arguments
    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, RecursionError) as e:
        # RecursionError: a string nested past the interpreter's limit is
        # still "not valid JSON" to the caller, not an internal error.
        raise ToolError(
            json.dumps(
                create_error_response(
                    code=ErrorCode.VALIDATION_INVALID_JSON,
                    message=f"'arguments' is a string but not valid JSON: {e}",
                    suggestions=[
                        "Pass 'arguments' as an object, not a JSON string.",
                    ],
                    context={"proxy_used": proxy_name, "tool_name": name},
                )
            )
        ) from e
    if not isinstance(parsed, dict):
        _raise_non_object_arguments(parsed, proxy_name, name)
    logger.warning(
        "Proxy %s received 'arguments' as a JSON string for tool %s — parsed as fallback",
        proxy_name,
        name,
    )
    return parsed


def _raise_wrong_category_error(
    name: str,
    transform: CategorizedSearchTransform,
    proxy_name: str,
) -> NoReturn:
    """Raise a ToolError naming the correct proxy for *name* (or not-found)."""
    # Provide a helpful error with the correct proxy name.
    # actual_category/correct_proxy are assigned in every branch below that
    # reaches the raise (the else branch raises early), so no initial sentinel
    # value is needed.
    correct_proxy = ""
    if name in transform._read_tools:
        actual_category: Capability = "read"
        correct_proxy = transform._call_read_name
    elif name in transform._write_tools:
        actual_category = "write"
        correct_proxy = transform._call_write_name
    elif name in transform._delete_tools:
        actual_category = "delete"
        correct_proxy = transform._call_delete_name
    else:
        raise ToolError(
            json.dumps(
                create_error_response(
                    code=ErrorCode.RESOURCE_NOT_FOUND,
                    message=f"Tool '{name}' not found. Use ha_search_tools to discover available tools.",
                    context={"tool_name": name},
                )
            )
        )
    raise ToolError(
        json.dumps(
            create_error_response(
                code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                message=f"Tool '{name}' is a {actual_category} tool. Use {correct_proxy} instead of {proxy_name}.",
                suggestions=[
                    f"Use '{correct_proxy}' for {actual_category} operations."
                ],
                context={
                    "tool_name": name,
                    "proxy_used": proxy_name,
                    "correct_proxy": correct_proxy,
                },
            )
        )
    )


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
        enable_code_mode: bool = False,
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
        # When code mode is enabled, the proxy must NOT dispatch to pinned
        # tools (specifically ``ha_manage_custom_tool``) — otherwise a
        # sandbox call to ``ha_call_write_tool`` with name=
        # "ha_manage_custom_tool" would launder a recursive invocation
        # past ``_BLOCKED_TOOLS`` inside the sandbox. Default False
        # preserves existing behaviour for installations that aren't
        # running code mode; server.py flips this on when
        # ``settings.enable_code_mode`` is True.
        self._enable_code_mode = enable_code_mode

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
        key = "|".join(sorted(f"{t.name}:{_categorize_tool(t)}" for t in tools))
        return hashlib.sha256(key.encode()).hexdigest()

    async def _rebuild_category_cache(self, ctx: Any) -> None:
        """Rebuild the read/write/delete category sets if catalog changed.

        When ``self._enable_code_mode`` is True, pinned tools are excluded
        from the category sets via ``_get_visible_tools`` (the same
        FastMCP helper that ``BM25SearchTransform`` uses). This prevents
        a sandbox-side recursive invocation laundered as
        ``ha_call_write_tool(name="ha_manage_custom_tool", ...)`` —
        without the filter, the pinned-and-callable
        ``ha_manage_custom_tool`` ends up in ``_write_tools`` and the
        proxy will happily dispatch.
        """
        if self._enable_code_mode:
            catalog = await self._get_visible_tools(ctx)
        else:
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
        proxy_map: dict[Capability, str] = {
            "read": self._call_read_name,
            "write": self._call_write_name,
            "delete": self._call_delete_name,
        }
        results = []
        for tool in tools:
            data = tool.to_mcp_tool().model_dump(mode="json", exclude_none=True)
            routes = _advertised_routes(tool.name, _categorize_tool(tool))
            if len(routes) == 1:
                data["execute_via"] = _execute_via(proxy_map[routes[0]], tool.name)
            else:
                hint = "; ".join(
                    f"{route} actions: {_execute_via(proxy_map[route], tool.name)}"
                    for route in routes
                )
                data["execute_via"] = hint[:1].upper() + hint[1:]
            results.append(data)
        return results

    def _make_categorized_proxy(
        self,
        proxy_name: str,
        category: Capability,
        annotations: ToolAnnotations,
        description: str,
    ) -> Tool:
        """Create a call proxy that validates tool category before execution."""
        transform = self

        async def categorized_call(
            name: Annotated[str, "The name of the tool to call"],
            arguments: Annotated[
                dict[str, Any] | str | None, "Arguments to pass to the tool"
            ] = None,
            ctx: Context = None,  # type: ignore[assignment]
        ) -> Any:
            # Rebuild category cache if catalog has changed
            await transform._rebuild_category_cache(ctx)

            # A client working from an older catalog can name a tool that has
            # since been renamed. The category check below reads the live
            # catalog, so it would reject that call before the re-dispatch
            # reaches RenamedToolAliasMiddleware — resolve the name here too,
            # and the alias covers both call shapes.
            requested = name
            name = current_tool_name(name)

            # Tolerate `arguments` passed as a JSON string — small models
            # sometimes serialize it before sending. Parse once up front so
            # downstream logic can assume a dict (or None).
            arguments = _coerce_proxy_arguments(arguments, proxy_name, name)
            # A retired name folded into an action-dispatched tool (#2329)
            # needs the action its old signature never carried.
            arguments = adapt_retired_arguments(requested, arguments)

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
                and name
                in (
                    transform._call_read_name,
                    transform._call_write_name,
                    transform._call_delete_name,
                )
            ):
                # The envelope carries whatever name the client knows the tool
                # by, and this check reads the live catalog — so resolve the
                # inner name for the same reason the outer one is resolved
                # above, or the alias covers one envelope shape and not the
                # other.
                requested_inner = arguments["name"]
                inner_name = current_tool_name(requested_inner)
                if inner_name in all_known:
                    logger.warning(
                        "Detected double-wrapped proxy call for '%s' via %s"
                        " — unwrapping",
                        inner_name,
                        name,
                    )
                    name = inner_name
                    # The nested payload gets the same treatment as the outer
                    # one: a JSON string is parsed, a scalar or list is
                    # refused with the same structured error rather than
                    # reaching the adapter, whose dict() would raise a bare
                    # ValueError, and a retired inner name gets its action.
                    nested = _coerce_proxy_arguments(
                        arguments.get("arguments"), proxy_name, inner_name
                    )
                    if nested is not None and not isinstance(nested, dict):
                        _raise_non_object_arguments(nested, proxy_name, inner_name)
                    arguments = adapt_retired_arguments(requested_inner, nested or {})

            if not _admits(transform, category, name, arguments):
                _raise_wrong_category_error(name, transform, proxy_name)

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
        search_tool = search_tool.model_copy(
            update={
                "description": self._search_tool_description or search_tool.description,
                "annotations": ToolAnnotations(openWorldHint=False, readOnlyHint=True),
            }
        )

        call_read = self._make_categorized_proxy(
            proxy_name=self._call_read_name,
            category="read",
            annotations=ToolAnnotations(openWorldHint=True, readOnlyHint=True),
            description=self._proxy_descs["read"],
        )

        call_write = self._make_categorized_proxy(
            proxy_name=self._call_write_name,
            category="write",
            annotations=ToolAnnotations(openWorldHint=True, destructiveHint=True),
            description=self._proxy_descs["write"],
        )

        call_delete = self._make_categorized_proxy(
            proxy_name=self._call_delete_name,
            category="delete",
            annotations=ToolAnnotations(openWorldHint=False, destructiveHint=True),
            description=self._proxy_descs["delete"],
        )

        if _read_only_mode():
            # ReadOnlyToolsTransform runs before this one and never sees the
            # proxies synthesised here; with every write blocked at call time
            # the destructive proxies would only advertise dead ends. They
            # stay resolvable by name so a client holding a stale catalog gets
            # the proxy's own structured answer (ReadOnlyMiddleware blocks a
            # write before the proxy is even resolved) rather than a bare
            # not-found that ToolSearchHintMiddleware declines to explain
            # while tool search is on.
            return [*pinned, search_tool, call_read]
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
                self._call_read_name,
                "read",
                ToolAnnotations(openWorldHint=True, readOnlyHint=True),
                self._proxy_descs["read"],
            )
        if name == self._call_write_name:
            return self._make_categorized_proxy(
                self._call_write_name,
                "write",
                ToolAnnotations(openWorldHint=True, destructiveHint=True),
                self._proxy_descs["write"],
            )
        if name == self._call_delete_name:
            return self._make_categorized_proxy(
                self._call_delete_name,
                "delete",
                ToolAnnotations(openWorldHint=False, destructiveHint=True),
                self._proxy_descs["delete"],
            )
        return await super().get_tool(name, call_next, version=version)
