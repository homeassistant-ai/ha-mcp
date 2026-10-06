"""The tool-search mode's ``ha_search_tools`` meta-tool (#1745, #2633).

A keyword search returns compact hits (name, description, one-line params);
``tools=[name]`` returns a tool's full input schema, the second hop the agent
makes before executing it with ``ha_call_tool``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.helpers import llm

from .llm_tool_metadata import declare_metadata, tool_result

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.util.json import JsonObjectType

# Names of the meta-tools synthesized for the tool-search mode. ha_search_tools
# deliberately matches the server's own tool-search terminology; if the server
# itself runs ENABLE_TOOL_SEARCH its identically-named tool is excluded from
# mirroring/search results to avoid duplicates.
SEARCH_TOOL_NAME = "ha_search_tools"
CALL_TOOL_NAME = "ha_call_tool"
_SEARCH_RESULT_LIMIT = 8
_NOT_FOUND_SUGGESTION = (
    f"Use {SEARCH_TOOL_NAME}(query=...) to discover available tools."
)


def _literal_values(node: dict[str, Any]) -> list[Any]:
    """The values an ``enum`` and/or ``const`` schema admits; empty otherwise."""
    values = list(node.get("enum") or [])
    if "const" in node:
        values.append(node["const"])
    return values


def compact_params(schema: Any) -> str:
    """Render an input schema as ``name (type[, required])`` joined by ``; ``."""

    def label(node: Any) -> str:
        if not isinstance(node, dict):
            return "any"
        kind = node.get("type")
        kinds = kind if isinstance(kind, list) else [kind]
        enum = _literal_values(node)
        if enum:
            nullable = "?" if None in enum or "null" in kinds else ""
            return "|".join(str(v) for v in enum if v is not None) + nullable
        if isinstance(kind, list):
            return label({"anyOf": [{**node, "type": k} for k in kind]})
        if kind == "array":
            return f"{label(node.get('items'))}[]"
        if isinstance(kind, str) and kind:
            return kind
        branches = node.get("anyOf") or node.get("oneOf")
        if isinstance(branches, list):
            labels = [label(b) for b in branches]
            typed = list(dict.fromkeys(x for x in labels if x != "null"))
            if not typed:
                return "null"
            return " | ".join(typed) + ("?" if "null" in labels else "")
        return "object" if {"$ref", "properties", "allOf"} & node.keys() else "any"

    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or not props:
        return "none"
    required = schema.get("required")
    required = set(required) if isinstance(required, list) else set()
    return "; ".join(
        f"{name} ({label(prop)}{', required' if name in required else ''})"
        for name, prop in props.items()
    )


def _search_score(query_words: list[str], name: str, description: str) -> int:
    """Score a tool against the query (simple word overlap + substring)."""
    haystack = f"{name} {description}".lower()
    name_lower = name.lower()
    score = 0
    for word in query_words:
        if word in name_lower:
            score += 3
        elif word in haystack:
            score += 1
    return score


class HaMcpSearchTool(llm.Tool):
    """Meta-tool: find ha-mcp tools relevant to a task (tool-search mode).

    Searches only the EXPOSED catalog snapshot taken at instance build, so a
    hidden tool can never appear in results, and a hidden name asked for by
    ``tools`` gets the same not-found entry a nonexistent one does.
    """

    name = SEARCH_TOOL_NAME
    description = (
        "Search the Home Assistant MCP toolset for tools relevant to a task. "
        "Returns each match's name, description, and compact params. Call "
        "with tools=[name] for the full input schema before executing a match "
        f"with {CALL_TOOL_NAME}."
    )
    parameters = vol.Schema({vol.Optional("query"): str, vol.Optional("tools"): [str]})

    def __init__(self, catalog: list[dict[str, Any]]) -> None:
        """Hold the exposed-catalog snapshot (name/description/schema dicts)."""
        self._catalog = catalog
        declare_metadata(
            self,
            title="Search HA-MCP Tools",
            hints={
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        )

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Return full entries for ``tools``, else compact hits for ``query``."""
        names = tool_input.tool_args.get("tools")
        if names:
            by_name = {t["name"]: t for t in self._catalog}
            return tool_result(
                {
                    "results": [
                        by_name.get(n)
                        or {
                            "name": n,
                            "error": f"Unknown tool '{n}'.",
                            "suggestion": _NOT_FOUND_SUGGESTION,
                        }
                        for n in names
                    ]
                },
                error=False,
            )
        query = str(tool_input.tool_args.get("query") or "")
        if not query.strip():
            return tool_result(
                {
                    "error": "Provide query (task words) or tools (tool names).",
                    "suggestion": (
                        f"{SEARCH_TOOL_NAME}(query='create automation') finds "
                        f"tools; {SEARCH_TOOL_NAME}(tools=[name]) returns one's "
                        "full input schema."
                    ),
                },
                error=True,
            )
        return self._search(query.lower().split())

    def _search(self, query_words: list[str]) -> JsonObjectType:
        """Return the top-scoring exposed tools as compact entries."""
        scored = sorted(
            (
                (_search_score(query_words, t["name"], t["description"]), t)
                for t in self._catalog
            ),
            key=lambda pair: pair[0],
            reverse=True,
        )
        results = [
            {
                "name": t["name"],
                "description": t["description"],
                "params": compact_params(t["input_schema"]),
            }
            for score, t in scored[:_SEARCH_RESULT_LIMIT]
            if score > 0
        ]
        if not results:
            return tool_result(
                {
                    "results": [],
                    "message": (
                        "No matching tools. Try different task words (e.g. "
                        "'automation', 'light', 'history', 'dashboard')."
                    ),
                },
                error=False,
            )
        return tool_result({"results": results}, error=False)
