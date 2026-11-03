"""Search/replace edits and content hashing for inline dashboard resources.

Lets ``ha_config_set_dashboard_resource(edits=...)`` change a few lines of a
large inline card without the model regenerating the whole payload. Edits are
exact-match and all-or-nothing: every ``old`` must occur exactly once in the
content as it stands when that edit is applied, or nothing is saved.
"""

from contextlib import AbstractAsyncContextManager, nullcontext
from typing import Annotated, Any, NoReturn, TypedDict

from pydantic import ConfigDict, Field

from ..errors import ErrorCode, create_error_response
from ..utils.registry_update_lock import registry_update_lock
from .helpers import raise_tool_error, ws_failure_code
from .resource_codec import (
    _DATA_URI_MIME,
    _MAX_ECHOED_URL,
    MAX_CONTENT_SIZE,
    _check_ws_error,
    _decode_inline_content,
    content_sha256,
)

MAX_EDITS = 100

# Lock key shared with _restore_dashboard_resource in backup_manager (the
# auto-backup domain name), so a restore and a tool write serialize too.
RESOURCE_LOCK_REGISTRY = "dashboard_resource"


def resource_write_lock(resource_id: str | None) -> AbstractAsyncContextManager[Any]:
    """Serialize this process's writes to one resource; a create needs none.

    ``with_auto_backup`` holds it around every dashboard_resource tool call,
    from the pre-write snapshot through the write. Writes from the HA UI or
    other clients are not serialized. Never take it inside those tools:
    ``asyncio.Lock`` is not reentrant.
    """
    if resource_id is None:
        return nullcontext()
    return registry_update_lock(RESOURCE_LOCK_REGISTRY, resource_id)


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
    # Bound the work before the loop: each edit scans and copies the whole
    # intermediate text, and the result's own size check only runs after it.
    added = 0
    for index, edit in enumerate(edits):
        try:
            added += len(edit["new"].encode("utf-8"))
        except UnicodeEncodeError:
            _raise_edit_error(
                "invalid_edit",
                f"Edit {index}: 'new' contains an unpaired surrogate, which "
                "is not valid UTF-8",
                {"edit_index": index, "invalid_edit_indexes": [index]},
                ["Send 'new' as valid Unicode text"],
            )
    if added > MAX_CONTENT_SIZE:
        _raise_edit_error(
            "edits_too_large",
            f"The edits' 'new' texts total {added:,} bytes (max {MAX_CONTENT_SIZE:,})",
            {"size": added, "limit": MAX_CONTENT_SIZE},
            ["Replace most of a resource with content= instead of edits"],
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


class ResourceLookupError(Exception):
    """Home Assistant rejected the resource listing or returned no list.

    ``result`` is Core's reply, so a caller can map Core's error code.
    """

    def __init__(self, detail: str, result: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.result = result


async def find_resource(
    client: Any, resource_id: str
) -> tuple[dict[str, Any], str | None] | None:
    """Look up ``resource_id`` in ``lovelace/resources``.

    Returns ``(row, decoded_content)``, with ``decoded_content`` None for a
    URL that is not an inline resource, or None when no row has that ID.
    Raises ``ResourceLookupError`` when Home Assistant rejects the listing or
    returns something other than a list; transport errors propagate as-is.
    Callers word their own errors.
    """
    result = await client.send_websocket_message({"type": "lovelace/resources"})
    error_msg = _check_ws_error(result)
    if error_msg:
        raise ResourceLookupError(
            f"Home Assistant rejected the listing: {error_msg}", result
        )
    resources = result.get("result") if isinstance(result, dict) else result
    if not isinstance(resources, list):
        raise ResourceLookupError(
            "Home Assistant returned an unreadable resource list", result
        )
    for row in resources:
        # A malformed entry must not raise a bare AttributeError.
        if isinstance(row, dict) and str(row.get("id")) == str(resource_id):
            url = row.get("url")
            decoded = _decode_inline_content(url) if isinstance(url, str) else None
            return row, decoded[0] if decoded else None
    return None


async def load_inline_resource(client: Any, resource_id: str) -> tuple[str, str]:
    """Fetch an inline resource and return ``(decoded_content, resource_type)``.

    Raises ``RESOURCE_NOT_FOUND`` for an unknown ID, a ``not_inline``
    validation error for /local/, /hacsfiles/, https:// or undecodable URLs,
    an ``unsupported_type`` validation error for an inline row whose type a
    ``data:`` URI cannot carry (legacy ``js``/``html`` worker rows), and
    Core's mapped error when the listing itself fails.
    """
    context = {"action": "patch", "resource_id": resource_id}
    try:
        found = await find_resource(client, resource_id)
    except ResourceLookupError as e:
        code = (
            ws_failure_code(e.result)
            if isinstance(e.result, dict)
            else ErrorCode.SERVICE_CALL_FAILED
        )
        raise_tool_error(create_error_response(code, e.detail, context=context))
    if found is None:
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
    stored, content = found
    if content is None:
        url = stored.get("url")
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
    resource_type = str(stored.get("type", "module"))
    if resource_type not in _DATA_URI_MIME:
        # Legacy worker URLs could carry 'js'/'html' rows; a data: URI cannot.
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Resource '{resource_id}' has type {resource_type!r}, which "
                "edits cannot rewrite as an inline resource",
                context={
                    **context,
                    "reason": "unsupported_type",
                    "resource_type": resource_type,
                },
                suggestions=[
                    "Pass the full content with resource_type='module' and "
                    "this resource_id to convert it",
                ],
            )
        )
    return content, resource_type


def check_config_hash(content: str, config_hash: str | None) -> None:
    """Refuse the patch when the stored content no longer has the caller's hash."""
    if config_hash is None:
        return
    current = content_sha256(content)
    if config_hash.strip().lower() != current:
        _raise_edit_error(
            "hash_mismatch",
            "The stored resource does not match config_hash; it changed "
            "since you last read it",
            {"current_config_hash": current},
            [
                "Re-read it with ha_config_list_dashboard_resources(include_hash=True, include_content=True)",
                "Rebuild the edits against the current content",
            ],
        )
