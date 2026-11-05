"""Blocking file operations and the edit_file service.

Kept out of ``__init__.py``, which is at its module-size baseline.

``edit_file`` runs here rather than on the server because only the component
sees a file's bytes: ``read_file`` shows a CRLF file with LF line endings, so a
server-side read and write would rewrite every line ending in the file.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import posixpath
import tempfile
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .volume_mounts import on_mounted_volume, os_error_text

_LOGGER = logging.getLogger(__name__)

SERVICE_EDIT_FILE = "edit_file"
MAX_EDITS = 100
# Each edit scans and copies the whole text, so bound the work up front.
MAX_EDITABLE_FILE_SIZE = 10 * 1024 * 1024

SERVICE_EDIT_FILE_SCHEMA = vol.Schema(
    {
        vol.Required("path"): cv.string,
        vol.Required("edits"): vol.All(
            [{vol.Required("old"): str, vol.Required("new"): str}],
            vol.Length(min=1, max=MAX_EDITS),
        ),
        vol.Optional("_ha_mcp_token"): cv.string,
    }
)


def _delete_file_sync(target_file: Path) -> dict[str, Any]:
    """Bundle delete_file blocking I/O for a single executor offload."""
    if not target_file.exists():
        return {"_error": "not_found"}
    if not target_file.is_file():
        return {"_error": "not_a_file"}
    stat = target_file.stat()
    target_file.unlink()
    return {"size": stat.st_size}


def _replace_file_sync(target_file: Path, content: str) -> dict[str, Any]:
    """Bundle whole-file replace I/O for a single executor offload.

    mkdir parent + atomic temp-write + rename + stat. Used by
    edit_yaml_config(action="replace_file").
    """
    target_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = target_file.with_suffix(".tmp")
    tmp_file.write_text(content)
    os.replace(str(tmp_file), str(target_file))
    stat = target_file.stat()
    return {"size": stat.st_size, "mtime": stat.st_mtime}


def _is_valid_utf8(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _occurrences(text: str, needle: str) -> int:
    """Count ``needle`` in ``text``, overlaps included.

    ``str.count`` skips overlaps ("aa" in "aaa" is 1), which would let an
    ambiguous ``old`` pass the exactly-once check. Not a regex lookahead: that
    rescans ``needle`` at every position while holding the GIL, which stalls
    Home Assistant's event loop on repetitive text even from an executor thread.
    """
    matches = 0
    position = -1
    while (position := text.find(needle, position + 1)) != -1:
        matches += 1
    return matches


def _with_file_newlines(text: str, crlf: bool) -> str:
    return text.replace("\r\n", "\n").replace("\n", "\r\n") if crlf else text


def _apply_edits(
    content: str, edits: list[dict[str, str]]
) -> tuple[str, dict[str, Any] | None]:
    """Apply ``edits`` in order; return ``(result, None)`` or ``("", refusal)``."""
    # Only a purely CRLF file can translate safely: in a mixed file the text
    # read_file shows does not say which line ending each line has.
    crlf = "\r\n" in content and "\n" not in content.replace("\r\n", "")
    translated = [
        (_with_file_newlines(edit["old"], crlf), _with_file_newlines(edit["new"], crlf))
        for edit in edits
    ]
    invalid = [
        index
        for index, (old, new) in enumerate(translated)
        if not old or old == new or not _is_valid_utf8(old + new)
    ]
    if invalid:
        return "", {
            "_error": "invalid_edit",
            "edit_index": invalid[0],
            "invalid_edit_indexes": invalid,
        }
    result = content
    for index, (old, new) in enumerate(translated):
        matches = _occurrences(result, old)
        if matches == 0:
            return "", {"_error": "old_not_found", "edit_index": index}
        if matches > 1:
            return "", {
                "_error": "old_not_unique",
                "edit_index": index,
                "matches": matches,
            }
        result = result.replace(old, new, 1)
    return result, None


def _replace_contents(target_file: Path, data: bytes) -> None:
    """Swap in ``data`` through a sibling temp file, keeping mode and owner.

    A failure before the swap leaves the file as it was. Resolves symlinks first,
    because ``os.replace`` would swap the link itself for a plain file. A target
    that is its own mount point fails the swap with ``EBUSY`` and stays intact.
    """
    real_file = Path(os.path.realpath(target_file))
    original = real_file.stat()
    handle, temp_name = tempfile.mkstemp(
        dir=real_file.parent, prefix=f".{real_file.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "wb") as temp_file:
            temp_file.write(data)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.chmod(temp_name, original.st_mode & 0o7777)
        if hasattr(os, "chown"):
            os.chown(temp_name, original.st_uid, original.st_gid)
        os.replace(temp_name, real_file)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


def edit_file_sync(
    target_file: Path,
    edits: list[dict[str, str]],
    volume_root_for: Callable[[str], str | None] = lambda _path: None,
) -> dict[str, Any]:
    """Bundle edit_file blocking I/O for a single executor offload.

    All-or-nothing: the file is written only after every edit has applied, and
    bytes outside the edited text are kept, line endings included.
    """
    root = volume_root_for(posixpath.normpath(str(target_file)))
    if root is not None and not on_mounted_volume(target_file, root):
        return {"_error": "not_mounted", "parent": root}
    if not target_file.exists():
        return {"_error": "not_found"}
    if not target_file.is_file():
        return {"_error": "not_a_file"}
    if target_file.stat().st_size > MAX_EDITABLE_FILE_SIZE:
        return {"_error": "too_large"}
    try:
        content = target_file.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return {"_error": "not_text"}
    result, refusal = _apply_edits(content, edits)
    if refusal is not None:
        return refusal
    data = result.encode("utf-8")
    if len(data) > MAX_EDITABLE_FILE_SIZE:
        return {"_error": "too_large"}
    _replace_contents(target_file, data)
    stat = target_file.stat()
    return {"size": stat.st_size, "mtime": stat.st_mtime}


_REFUSALS = {
    "not_found": "File not found: {path}. Pass content to create it.",
    "not_a_file": "Not a file: {path}.",
    "not_text": "{path} is not UTF-8 text, so it cannot be edited.",
    "too_large": "{path} would be too large to edit. Nothing was saved.",
    "not_mounted": "{parent} is not mounted into Home Assistant Core on this install.",
    "invalid_edit": "Edits at index {invalid_edit_indexes} need a non-empty 'old' "
    "that differs from 'new', both valid UTF-8. Nothing was saved.",
    "old_not_found": "Edit {edit_index}: 'old' text not found in {path}. Match it "
    "exactly, including whitespace and line endings; earlier edits apply first. "
    "Nothing was saved.",
    "old_not_unique": "Edit {edit_index}: 'old' text occurs {matches} times in "
    "{path}. Add surrounding context so it matches once. Nothing was saved.",
}


def build_edit_file_handler(
    hass: HomeAssistant,
    *,
    caller_token_ok: Callable[[HomeAssistant, ServiceCall], bool],
    unauthorized_response: Callable[[str], dict[str, Any]],
    write_denied: Callable[[HomeAssistant, str], str | None],
    volume_root_for: Callable[[str], str | None],
) -> Callable[[ServiceCall], Awaitable[ServiceResponse]]:
    """Build the edit_file service handler."""
    config_dir = Path(hass.config.config_dir)
    # One lock for all files: overlapping read-modify-writes of one file would
    # lose an edit and report success for both, and edits are too rare for the
    # bookkeeping of per-path locks (and of links to one file) to pay off.
    edit_lock = asyncio.Lock()

    async def handle_edit_file(call: ServiceCall) -> ServiceResponse:
        if not caller_token_ok(hass, call):
            return unauthorized_response(SERVICE_EDIT_FILE)
        rel_path = call.data["path"]
        edits = call.data["edits"]
        denied = write_denied(hass, rel_path)
        if denied is not None:
            return {"success": False, "error": denied}
        try:
            async with edit_lock:
                result = await hass.async_add_executor_job(
                    edit_file_sync, config_dir / rel_path, edits, volume_root_for
                )
        except PermissionError:
            _LOGGER.error("Permission denied editing: %s", rel_path)
            return {"success": False, "error": f"Permission denied: {rel_path}"}
        except OSError as err:
            _LOGGER.error("Error editing file %s: %s", rel_path, err)
            return {"success": False, "error": os_error_text(err, rel_path)}
        reason = result.pop("_error", None)
        if reason is not None:
            return {
                "success": False,
                "error": _REFUSALS[reason].format(path=rel_path, **result),
                "reason": reason,
                **result,
            }
        _LOGGER.info("Edited file: %s (%d bytes)", rel_path, result["size"])
        return {
            "success": True,
            "path": rel_path,
            "size": result["size"],
            "modified": datetime.fromtimestamp(result["mtime"]).isoformat(),
            "edits_applied": len(edits),
            "message": "File updated successfully",
        }

    return handle_edit_file


def register_edit_file(
    hass: HomeAssistant,
    *,
    caller_token_ok: Callable[[HomeAssistant, ServiceCall], bool],
    unauthorized_response: Callable[[str], dict[str, Any]],
    write_denied: Callable[[HomeAssistant, str], str | None],
    volume_root_for: Callable[[str], str | None],
) -> None:
    """Register edit_file on the tools entry."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_EDIT_FILE,
        build_edit_file_handler(
            hass,
            caller_token_ok=caller_token_ok,
            unauthorized_response=unauthorized_response,
            write_denied=write_denied,
            volume_root_for=volume_root_for,
        ),
        schema=SERVICE_EDIT_FILE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
