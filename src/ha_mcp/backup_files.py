"""Auto-backup fetch and restore for whole files (``file`` / ``yaml_file``).

Kept out of ``backup_manager.py``, which is at its module-size baseline.
"""

from __future__ import annotations

from typing import Any

from .client.rest_client import HomeAssistantError

# The component flag that gates read_file's ``keep_line_endings`` field.
LINE_ENDINGS_CAPABILITY = "read_file_line_endings"


async def _fetch_file(
    client: Any, entity_id: str, *, keep_line_endings: bool = False
) -> Any:
    """Read a file's current content via the read_file service.

    ``entity_id`` is the file path. Returns the content string, or None
    when the file does not exist — a brand-new write has no prior content
    to snapshot, so capture skips (same as creating a new entity). A
    binary file also returns None: snapshots store text only, so capture
    skips it instead of blocking the write or delete. Other read failures
    raise so the capture pipeline logs them at WARNING.
    """
    from .tools.auto_backup import PathNotAllowedError
    from .tools.tools_filesystem import call_mcp_tools_service
    from .tools.util_helpers import unwrap_service_response

    service_data: dict[str, Any] = {"path": entity_id}
    if keep_line_endings:
        service_data["keep_line_endings"] = True
    result = await call_mcp_tools_service(client, "read_file", service_data)
    if not isinstance(result, dict):
        return None
    result = unwrap_service_response(result)
    if result.get("success", False):
        content = result.get("content")
        return content if isinstance(content, str) else None
    error = str(result.get("error", ""))
    skips = ("does not exist", "not a file", "Cannot read binary file")
    if any(marker in error for marker in skips):
        return None
    if error.startswith("Path not allowed"):
        raise PathNotAllowedError(entity_id, error)
    raise HomeAssistantError(f"read_file failed for {entity_id!r}: {error}")


async def _restore_file(client: Any, entity_id: str, config: Any) -> Any:
    """Re-write a file's captured content via the write_file service."""
    from .tools.tools_filesystem import call_mcp_tools_service
    from .tools.util_helpers import unwrap_service_response

    result = await call_mcp_tools_service(
        client,
        "write_file",
        {
            "path": entity_id,
            "content": str(config),
            "overwrite": True,
            "create_dirs": True,
        },
    )
    if isinstance(result, dict):
        result = unwrap_service_response(result)
        if not result.get("success", False):
            raise HomeAssistantError(
                f"write_file restore failed for {entity_id!r}: {result.get('error')}"
            )
    return result


async def _fetch_file_as_stored(client: Any, entity_id: str) -> Any:
    """``_fetch_file`` for a snapshot: CRLF stays CRLF, so a restore is exact.

    A component without the ``read_file_line_endings`` flag would refuse the
    field, so it gets the LF read it always served.
    """
    from .tools.component_api import component_supports, get_component_caps

    caps = await get_component_caps(client)
    return await _fetch_file(
        client,
        entity_id,
        keep_line_endings=component_supports(caps, LINE_ENDINGS_CAPABILITY),
    )
