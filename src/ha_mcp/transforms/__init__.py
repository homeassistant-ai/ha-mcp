"""Custom FastMCP transforms for ha-mcp."""

from .categorized_search import (
    DEFAULT_PINNED_TOOLS,
    Capability,
    CategorizedSearchTransform,
    SearchKeywordsTransform,
    categorize_capability,
)
from .component_search import ComponentSearchSchemaTransform
from .lite_docstrings import LiteDocstringsTransform
from .write_tool_note import DESKTOP_APPROVAL_NOTE, WriteToolNoteTransform

__all__ = [
    "Capability",
    "CategorizedSearchTransform",
    "ComponentSearchSchemaTransform",
    "DEFAULT_PINNED_TOOLS",
    "DESKTOP_APPROVAL_NOTE",
    "LiteDocstringsTransform",
    "SearchKeywordsTransform",
    "WriteToolNoteTransform",
    "categorize_capability",
]
