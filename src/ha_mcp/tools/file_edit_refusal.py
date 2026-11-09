"""Turn an edit_file refusal into the structured error the edit tools share.

``ha_config_set_dashboard_resource(edits=...)`` raises its refusals through
``create_error_response`` with ``reason`` in the context; ``ha_write_file``
edits do the same, so both edit surfaces answer in one shape.
"""

from typing import Any, NoReturn

from ..errors import ErrorCode, create_error_response
from .helpers import raise_tool_error

# Refusals of the edits themselves; any other reason is about the file or call.
_EDIT_REASONS = frozenset(
    {"invalid_edit", "old_not_found", "old_not_unique", "line_endings_only"}
)
_CONTEXT_KEYS = (
    "reason",
    "edit_index",
    "invalid_edit_indexes",
    "matches",
    "line_endings",
    "parent",
)
_SUGGESTIONS = {
    "invalid_edit": ["Every edit needs a non-empty 'old' that differs from its 'new'"],
    "old_not_found": [
        "Match the stored text exactly, including whitespace",
        "Earlier edits apply first; account for text they changed",
        "Read the current text with ha_read_file",
    ],
    "old_not_unique": ["Add surrounding context to 'old' so it matches once"],
    "line_endings_only": ["Send the whole file with content= to change line endings"],
    "not_found": ["Create the file with content= instead of edits"],
}


def raise_edit_file_refusal(result: dict[str, Any], path: str) -> NoReturn:
    """Raise the component's ``success: false`` edit_file reply as a ToolError."""
    reason = result.get("reason")
    code = (
        ErrorCode.VALIDATION_INVALID_PARAMETER
        if reason in _EDIT_REASONS
        else ErrorCode.SERVICE_CALL_FAILED
    )
    context: dict[str, Any] = {"path": path}
    context.update({key: result[key] for key in _CONTEXT_KEYS if key in result})
    raise_tool_error(
        create_error_response(
            code,
            str(result.get("error") or "edit_file failed"),
            suggestions=_SUGGESTIONS.get(str(reason)),
            context=context,
        )
    )
