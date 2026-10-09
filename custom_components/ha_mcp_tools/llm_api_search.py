"""The tool-search mode's ``ha_search_tools`` meta-tool (#1745, #2633).

A keyword search returns compact hits (name, one-line description, one-line
params); ``tools=[name]`` returns a tool's full description and input schema,
the second hop the agent makes before executing it with ``ha_call_tool``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.helpers import llm

from .llm_tool_metadata import declare_metadata, tool_result

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.util.json import JsonObjectType

# Names of the meta-tools synthesized for the tool-search mode. ha_search_tools
# deliberately matches the server's own tool-search terminology (see the
# exposure filter in llm_api.py for the duplicate-name rule).
SEARCH_TOOL_NAME = "ha_search_tools"
CALL_TOOL_NAME = "ha_call_tool"
_SEARCH_RESULT_LIMIT = 8
_NOT_FOUND_SUGGESTION = (
    f"Use {SEARCH_TOOL_NAME}(query=...) to discover available tools."
)


# The compact-params renderer below is a verbatim copy of the server's
# (src/ha_mcp/transforms/categorized_search.py); this package cannot import
# ha_mcp. tests/src/unit/test_llm_api_search.py checks the two agree.
def _literal_labels(branch: dict[str, Any]) -> list[str]:
    """Labels of the ``enum``/``const`` values *branch* admits; ``None`` is
    kept as-is for the caller's nullable check."""
    values = [*(branch.get("enum") or [])]
    if "const" in branch:
        values.append(branch["const"])
    return [v if isinstance(v, str) or v is None else json.dumps(v) for v in values]


def _plain_type(branch: dict[str, Any], *, nested: bool) -> str:
    """Label of a branch without literal values."""
    kind = branch.get("type")
    if kind == "array":
        return f"{_param_type(branch.get('items'))}[]"
    if isinstance(kind, str) and kind:
        return kind
    if nested and (isinstance(kind, list) or "anyOf" in branch or "oneOf" in branch):
        return _param_type(branch)
    return "object" if {"$ref", "properties", "allOf"} & branch.keys() else "any"


def _param_type(node: Any) -> str:
    """Type label for one parameter: ``|`` joins union branches, enum and
    const values are spelled out inline, ``?`` marks nullable, ``T[]`` an
    array of ``T``."""
    if not isinstance(node, dict):
        return "any"
    kind = node.get("type")
    if isinstance(kind, list):
        branches: list[Any] = [{**node, "type": k} for k in kind]
    else:
        branches = node.get("anyOf") or node.get("oneOf") or [node]
    labels: dict[str, None] = {}
    nullable = False
    for branch in branches:
        if not isinstance(branch, dict):
            continue
        if values := _literal_labels(branch):
            nullable = nullable or None in values
            labels.update(dict.fromkeys(v for v in values if v is not None))
        elif branch.get("type") == "null":
            nullable = True
        else:
            labels[_plain_type(branch, nested=branch is not node)] = None
    if not labels:
        return "null" if nullable else "any"
    return "|".join(labels) + ("?" if nullable else "")


def compact_params(schema: Any) -> str:
    """One line naming every parameter with its type and required marker;
    ``none`` for a tool without parameters."""
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or not props:
        return "none"
    required = schema.get("required")
    required = set(required) if isinstance(required, list) else set()
    return "; ".join(
        f"{name} ({_param_type(field)}{', required' if name in required else ''})"
        for name, field in props.items()
    )


def summary(description: str | None) -> str:
    """The first paragraph of a tool description, on one line.

    The paragraphs after it (and the server's appended BM25 keyword list)
    come back with the full entry from ``tools=[...]``.
    """
    return " ".join((description or "").split("\n\n", 1)[0].split())


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
    ``tools`` gets the same not-found entry a nonexistent one does. A pinned
    tool is already in the agent's tool list with its schema, so it comes
    back as a name-only stub on both paths (#2576).
    """

    name = SEARCH_TOOL_NAME
    description = (
        "Search the Home Assistant MCP toolset for tools relevant to a task. "
        "Returns each match's name, one-line description, and compact params. "
        "Call with tools=[name] for the full description and input schema "
        f"before executing a match with {CALL_TOOL_NAME}."
    )
    parameters = vol.Schema({vol.Optional("query"): str, vol.Optional("tools"): [str]})

    def __init__(self, catalog: list[dict[str, Any]], pinned: set[str]) -> None:
        """Hold the exposed-catalog snapshot (name/description/schema dicts)."""
        self._catalog = catalog
        self._pinned = pinned
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
        """Return full entries for ``tools``, compact hits for ``query``, or an
        error when neither is given.

        Core hands ``tool_args`` over without checking them against
        ``parameters``, so ``tools`` is normalised here: a bare name is
        accepted as a one-item list, anything but a list of names is an
        error.
        """
        names = tool_input.tool_args.get("tools")
        if isinstance(names, str):
            names = [names]
        if names is not None and not (
            isinstance(names, list) and all(isinstance(n, str) for n in names)
        ):
            return tool_result(
                {
                    "error": "tools must be a list of tool names.",
                    "suggestion": f"{SEARCH_TOOL_NAME}(tools=['ha_get_state'])",
                },
                error=True,
            )
        if names:
            return self._full(names)
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

    def _stub(self, name: str) -> dict[str, Any]:
        return {
            "name": name,
            "pinned": True,
            "hint": f"{name} is already in your tool list — call it directly.",
        }

    def _full(self, names: list[str]) -> JsonObjectType:
        """Full entries for *names* in the order given; an error when none
        of them is an exposed tool."""
        by_name = {t["name"]: t for t in self._catalog}
        results = [
            self._stub(n)
            if n in self._pinned and n in by_name
            else by_name.get(n)
            or {
                "name": n,
                "error": f"Unknown tool '{n}'.",
                "suggestion": _NOT_FOUND_SUGGESTION,
            }
            for n in names
        ]
        return tool_result(
            {"results": results},
            error=not any(n in by_name for n in names),
        )

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
            self._stub(t["name"])
            if t["name"] in self._pinned
            else {
                "name": t["name"],
                "description": summary(t["description"]),
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
