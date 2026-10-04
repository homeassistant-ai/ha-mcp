"""Core tool metadata and results for the LLM API's tools (#1745).

Home Assistant 2026.10 added ``llm.ToolAnnotations``, ``llm.ToolResult``, and
the ``title``/``annotations``/``integration`` tool attributes. Everything here
feature-detects them, so Core 2026.8/2026.9 behave exactly as before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from homeassistant.helpers import llm

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.util.json import JsonObjectType

# MCP hint -> llm.ToolAnnotations field. Home Assistant 2026.10 added both
# llm.ToolAnnotations and llm.ToolResult; older cores have neither.
_HINT_FIELDS = (
    ("readOnlyHint", "read_only"),
    ("destructiveHint", "destructive"),
    ("idempotentHint", "idempotent"),
    ("openWorldHint", "open_world"),
)


def declare_metadata(
    tool: llm.Tool, *, title: str | None, hints: dict[str, Any] | None
) -> None:
    """Set the 2026.10 tool metadata; a hint left out keeps Core's safe default."""
    tool.integration = DOMAIN
    tool.title = title
    annotations_cls = getattr(llm, "ToolAnnotations", None)
    if annotations_cls is not None and hints:
        tool.annotations = annotations_cls(
            **{field: hints[hint] for hint, field in _HINT_FIELDS if hint in hints}
        )


def tool_result(data: JsonObjectType, *, error: bool) -> Any:
    """Wrap a result in ``llm.ToolResult`` where Core has it, else return it bare."""
    result_cls = getattr(llm, "ToolResult", None)
    return data if result_cls is None else result_cls(data=data, error=error)


def tool_hints(tool: Any) -> dict[str, Any] | None:
    """The tool's MCP annotations by wire name, on either SDK line."""
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return None
    return cast(
        "dict[str, Any]", annotations.model_dump(by_alias=True, exclude_none=True)
    )


def tool_title(tool: Any) -> str | None:
    """The server's display title: ``title``, else ``annotations.title``."""
    return getattr(tool, "title", None) or (tool_hints(tool) or {}).get("title")
