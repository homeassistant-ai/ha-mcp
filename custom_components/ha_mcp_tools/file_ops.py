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
from collections.abc import Awaitable, Callable, Coroutine
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
from homeassistant.helpers import service as ha_service

from .const import DOMAIN
from .file_post_action import after_file_write
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


def file_lock(hass: HomeAssistant) -> asyncio.Lock:
    """The one lock every file change in this component takes.

    edit_yaml_config holds it across its read-modify-write too (``run_locked``): a
    theme file is writable through both it and edit_file. Not reentrant, so
    nothing holding it may call ``run_file_job``.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    lock: asyncio.Lock = domain_data.setdefault("file_lock", asyncio.Lock())
    return lock


async def run_file_job(
    hass: HomeAssistant, job: Callable[..., dict[str, Any]], *args: Any
) -> dict[str, Any]:
    """Run edit_file, write_file or delete_file I/O in the executor, one at a time.

    An edit reads and then swaps in its result, so an overlapping write or delete
    would be undone while both calls report success. One lock for all files: edits
    are too rare for the bookkeeping of per-path locks (and of links to one file)
    to pay off.
    """
    return await run_locked(hass, lambda: hass.async_add_executor_job(job, *args))


async def run_locked[T](hass: HomeAssistant, start: Callable[[], Awaitable[T]]) -> T:
    """Run the work ``start`` begins under ``file_lock``, to its end.

    A running thread can't be stopped, so a cancelled caller keeps the lock until
    the work ends: otherwise the next write could report success before the
    cancelled one overwrites it. edit_yaml_config runs its whole edit this way.
    """
    async with file_lock(hass):
        running = asyncio.ensure_future(start())
        try:
            return await asyncio.shield(running)
        except asyncio.CancelledError:
            while not running.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait({running})
            raise


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

    mkdir parent + atomic swap + stat. Used by
    edit_yaml_config(action="replace_file").
    """
    target_file.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(target_file, content)
    stat = target_file.stat()
    return {"size": stat.st_size, "mtime": stat.st_mtime}


def write_text_atomic(target_file: Path, content: str) -> None:
    """Write ``content`` as stored, swapping an existing file in atomically.

    An existing file goes through ``_replace_contents``, so a sibling file, a
    symlink and the file's mode survive. A new file has nothing to protect and
    is created in place; a failed write removes it again.
    """
    data = content.encode("utf-8")
    if target_file.exists():
        _replace_contents(target_file, data)
        return
    with open(target_file, "xb") as new_file:
        try:
            new_file.write(data)
        except BaseException:
            new_file.close()
            with contextlib.suppress(OSError):
                target_file.unlink()
            raise


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


def _line_endings(content: str) -> str:
    """``crlf``, ``lf`` or ``mixed``: how the lines of ``content`` end."""
    has_crlf = "\r\n" in content
    has_lf = "\n" in content.replace("\r\n", "")
    if has_crlf and has_lf:
        return "mixed"
    return "crlf" if has_crlf else "lf"


def _apply_edits(
    content: str, edits: list[dict[str, str]]
) -> tuple[str, dict[str, Any] | None]:
    """Apply ``edits`` in order; return ``(result, None)`` or ``("", refusal)``."""
    # Only a purely CRLF file can translate safely: in a mixed file the text
    # read_file shows does not say which line ending each line has.
    endings = _line_endings(content)
    crlf = endings == "crlf"
    invalid = [
        index
        for index, edit in enumerate(edits)
        if not edit["old"]
        or edit["old"] == edit["new"]
        or not _is_valid_utf8(edit["old"] + edit["new"])
    ]
    if invalid:
        return "", {
            "_error": "invalid_edit",
            "edit_index": invalid[0],
            "invalid_edit_indexes": invalid,
        }
    translated = [
        (_with_file_newlines(edit["old"], crlf), _with_file_newlines(edit["new"], crlf))
        for edit in edits
    ]
    # Edits keep a CRLF file's line endings, so one that differs from its 'old'
    # only in line endings would save nothing.
    same = [index for index, (old, new) in enumerate(translated) if old == new]
    if same:
        return "", {"_error": "line_endings_only", "edit_index": same[0]}
    result = content
    for index, (old, new) in enumerate(translated):
        matches = _occurrences(result, old)
        if matches == 0:
            refusal: dict[str, Any] = {"_error": "old_not_found", "edit_index": index}
            if endings == "mixed" and "\n" in old:
                refusal["line_endings"] = "mixed"
            return "", refusal
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
        # A fixed prefix: the target's own name may already be at the filesystem's
        # limit, and a temp name built from it would make that file uneditable.
        dir=real_file.parent,
        prefix=".ha_mcp_edit.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(handle, "wb") as temp_file:
            temp_file.write(data)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.chmod(temp_name, original.st_mode & 0o7777)
        if hasattr(os, "chown"):
            # A user that may write a file it does not own cannot give the
            # copy that owner; the file then belongs to Home Assistant's user.
            with contextlib.suppress(PermissionError):
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
    "line_endings_only": "Edit {edit_index} changes only line endings, which edits "
    "keep as {path} stores them. Send the whole file with content to change them. "
    "Nothing was saved.",
}

# Appended to the refusal when the file mixes CRLF and LF line endings.
_MIXED_LINE_ENDINGS = (
    " {path} mixes CRLF and LF line endings: a multi-line 'old' matches only "
    "lines stored with the line ending it uses."
)


def refusal_text(reason: str, path: str, details: dict[str, Any]) -> str:
    """The message for a refused edit, ``details`` being its ``_error`` fields."""
    text = _REFUSALS[reason].format(path=path, **details)
    if details.get("line_endings") == "mixed":
        text += _MIXED_LINE_ENDINGS.format(path=path)
    return text


def build_edit_file_handler(
    hass: HomeAssistant,
    *,
    caller_token_ok: Callable[[HomeAssistant, ServiceCall], bool],
    unauthorized_response: Callable[[str], dict[str, Any]],
    write_denied: Callable[[HomeAssistant, str], str | None],
    volume_root_for: Callable[[str], str | None],
) -> Callable[[ServiceCall], Coroutine[Any, Any, ServiceResponse]]:
    """Build the edit_file service handler."""
    config_dir = Path(hass.config.config_dir)

    async def handle_edit_file(call: ServiceCall) -> ServiceResponse:
        if not caller_token_ok(hass, call):
            return unauthorized_response(SERVICE_EDIT_FILE)
        rel_path = call.data["path"]
        edits = call.data["edits"]
        denied = write_denied(hass, rel_path)
        if denied is not None:
            return {"success": False, "error": denied}
        try:
            result = await run_file_job(
                hass, edit_file_sync, config_dir / rel_path, edits, volume_root_for
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
                "error": refusal_text(reason, rel_path, result),
                "reason": reason,
                **result,
            }
        _LOGGER.info("Edited file: %s (%d bytes)", rel_path, result["size"])
        response: dict[str, Any] = {
            "success": True,
            "path": rel_path,
            "size": result["size"],
            "modified": datetime.fromtimestamp(result["mtime"]).isoformat(),
            "edits_applied": len(edits),
            "message": "File updated successfully",
        }
        response.update(await after_file_write(hass, rel_path, result["size"]))
        return response

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
    ha_service.async_register_admin_service(
        hass,
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
