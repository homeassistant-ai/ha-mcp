"""Search/replace edits and content hashing for inline dashboard resources.

Lets ``ha_config_set_dashboard_resource(edits=...)`` change a few lines of a
large inline card without the model regenerating the whole payload. Edits are
exact-match and all-or-nothing: every ``old`` must occur exactly once in the
content as it stands when that edit is applied, or nothing is saved.
"""

from typing import Annotated, Any, NoReturn, TypedDict

from pydantic import ConfigDict, Field

from ..errors import ErrorCode, create_error_response
from .helpers import raise_tool_error, ws_failure_code
from .resource_codec import (
    _MAX_ECHOED_URL,
    _check_ws_error,
    _decode_inline_content,
    content_sha256,
)

MAX_EDITS = 100


class ResourceEdit(TypedDict):
    """One exact-match replacement in an inline resource."""

    old: Annotated[
        str,
        Field(
            description="Exact text to replace. Must occur exactly once in the "
            "content at the point this edit is applied; include surrounding "
            "context to make it unique."
        ),
    ]
    new: Annotated[str, Field(description="Replacement text (may be empty).")]


# Pydantic reads this at schema time; mypy allows only fields in the body.
ResourceEdit.__pydantic_config__ = ConfigDict(extra="forbid")  # type: ignore[attr-defined]


def _count_occurrences(haystack: str, needle: str) -> int:
    """Count occurrences of ``needle``, overlapping ones included.

    ``str.count`` skips overlaps ("aa" in "aaa" is 1), which would let an
    ambiguous ``old`` pass the exactly-once check.
    """
    count = 0
    start = haystack.find(needle)
    while start != -1:
        count += 1
        start = haystack.find(needle, start + 1)
    return count


def _raise_edit_error(
    reason: str, message: str, context: dict[str, Any], suggestions: list[str]
) -> NoReturn:
    raise_tool_error(
        create_error_response(
            code=ErrorCode.VALIDATION_INVALID_PARAMETER,
            message=f"{message}. Nothing was saved.",
            context={"reason": reason, **context},
            suggestions=suggestions,
        )
    )


def apply_resource_edits(content: str, edits: list[ResourceEdit]) -> str:
    """Apply ``edits`` in order to a copy of ``content`` and return the result.

    Raises a structured ``ToolError`` (``reason`` in its context) on the first
    edit that cannot apply, so the caller never sees a partial result.
    """
    invalid = [
        index
        for index, edit in enumerate(edits)
        if not edit["old"] or edit["old"] == edit["new"]
    ]
    if invalid:
        _raise_edit_error(
            "invalid_edit",
            f"Edit(s) at index {', '.join(map(str, invalid))} have an empty "
            "'old' or an 'old' identical to 'new'",
            {"edit_index": invalid[0], "invalid_edit_indexes": invalid},
            ["Every edit needs a non-empty 'old' that differs from its 'new'"],
        )

    result = content
    for index, edit in enumerate(edits):
        matches = _count_occurrences(result, edit["old"])
        if matches == 0:
            _raise_edit_error(
                "old_not_found",
                f"Edit {index}: 'old' text not found in the resource",
                {"edit_index": index},
                [
                    "Match the stored text exactly, including whitespace and quotes",
                    "Earlier edits apply first; account for text they changed",
                    "Fetch the current code with ha_config_list_dashboard_resources(include_content=True)",
                ],
            )
        if matches > 1:
            _raise_edit_error(
                "old_not_unique",
                f"Edit {index}: 'old' text occurs {matches} times in the resource",
                {"edit_index": index, "matches": matches},
                ["Extend 'old' with surrounding context so it matches exactly once"],
            )
        result = result.replace(edit["old"], edit["new"], 1)
    return result


async def load_inline_resource(client: Any, resource_id: str) -> tuple[str, str]:
    """Fetch an inline resource and return ``(decoded_content, resource_type)``.

    Raises ``RESOURCE_NOT_FOUND`` for an unknown ID and a ``not_inline``
    validation error for /local/, /hacsfiles/, https:// or undecodable URLs.
    """
    context = {"action": "patch", "resource_id": resource_id}
    result = await client.send_websocket_message({"type": "lovelace/resources"})
    error_msg = _check_ws_error(result)
    if error_msg:
        raise_tool_error(
            create_error_response(ws_failure_code(result), error_msg, context=context)
        )
    resources = result.get("result") if isinstance(result, dict) else result
    stored = next(
        (
            res
            for res in (resources if isinstance(resources, list) else [])
            if isinstance(res, dict) and str(res.get("id")) == str(resource_id)
        ),
        None,
    )
    if stored is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Dashboard resource '{resource_id}' not found",
                context=context,
                suggestions=[
                    "Use ha_config_list_dashboard_resources() to see available resources"
                ],
            )
        )
    url = stored.get("url")
    decoded = _decode_inline_content(url) if isinstance(url, str) else None
    if decoded is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Resource '{resource_id}' is not an inline resource, so it "
                "cannot be patched with edits",
                context={
                    **context,
                    "reason": "not_inline",
                    "url": url[:_MAX_ECHOED_URL] if isinstance(url, str) else url,
                },
                suggestions=[
                    "Edit the file behind a /local/ URL with ha_write_file instead",
                    "HACS and CDN resources are managed by their source",
                ],
            )
        )
    return decoded[0], str(stored.get("type", "module"))


def check_expected_sha256(content: str, expected_sha256: str | None) -> None:
    """Refuse the patch when the stored content no longer has the caller's hash."""
    if expected_sha256 is None:
        return
    actual = content_sha256(content)
    if expected_sha256.strip().lower() != actual:
        _raise_edit_error(
            "hash_mismatch",
            "The stored resource does not match expected_sha256; it changed "
            "since you last read it",
            {"actual_sha256": actual},
            [
                "Re-read it with ha_config_list_dashboard_resources(include_hash=True, include_content=True)",
                "Rebuild the edits against the current content",
            ],
        )
