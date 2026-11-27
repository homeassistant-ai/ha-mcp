"""The file Home Assistant writes its log to, as ``read_file`` reads it (#2632)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from homeassistant.bootstrap import ERROR_LOG_FILENAME
from homeassistant.const import KEY_DATA_LOGGING, KEY_DATA_LOGGING_DISABLED_REASON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.hassio import is_hassio


def read_target(
    hass: HomeAssistant, config_dir: Path, rel_path: str
) -> tuple[Path | None, str]:
    """The file a read of ``rel_path`` opens (``None``: none), and what a
    not-found answer adds to the path."""
    if os.path.normpath(rel_path) != ERROR_LOG_FILENAME:
        return config_dir / rel_path, ""
    target = _error_log_file(hass)
    if isinstance(target, str):
        return None, f". {target}"
    return target, f". Core logs to {target}"


def _error_log_file(hass: HomeAssistant) -> Path | str:
    """The file Core writes its log to, or why it writes none.

    ``--log-file`` moves the file. Supervisor (without
    ``HA_DUPLICATE_LOG_FILE``) and ``HA_DISABLE_LOG_FILE`` turn it off, and
    Core stores a reason only for the second. Supervisor is told by
    ``is_hassio``, as Core's own ``system_log/llm.py`` does.
    """
    if (path := hass.data.get(KEY_DATA_LOGGING)) is not None:
        return Path(path)
    if reason := hass.data.get(KEY_DATA_LOGGING_DISABLED_REASON):
        return f"Home Assistant's log file is disabled (reason: {reason})."
    if is_hassio(hass):
        return (
            "Under Supervisor, Home Assistant logs to its journald stream, "
            "which ha_get_logs(source='error_log') reads."
        )
    return "Home Assistant writes no log file."


def read_allowlist_applies(hass: HomeAssistant, name: str) -> bool:
    """Whether the read allowlist guards ``name``.

    Core's log is checked only at Core's default path: a ``--log-file`` path
    is the one Core was started with, and with no file log nothing is opened.
    """
    if name != ERROR_LOG_FILENAME:
        return True
    return hass.data.get(KEY_DATA_LOGGING) == hass.config.path(ERROR_LOG_FILENAME)


def read_log_tail_sync(
    target_file: Path, tail_lines: int, *, chunk_size: int = 1 << 16
) -> dict[str, Any]:
    """Tail a log for one executor offload without materialising the whole file.

    Core's log grows until Core's next start (or midnight, with
    ``--log-rotate-days``), the fault log for the life of the install, so
    reading a file whole just to keep its last ``tail_lines`` lines scales
    with its history rather than the request. Line count streams
    the file in chunks; the tail seeks back from the end until it holds
    ``tail_lines`` newlines. The result carries ``content`` / ``total_lines`` /
    ``truncated`` with ``split("\n")`` semantics (a trailing newline yields one
    empty last line), plus ``size``/``mtime`` like ``_read_file_sync``. Unlike
    that whole-file read, bytes that are not UTF-8 are escaped rather than
    failing the read.
    """
    if not target_file.exists():
        return {"_error": "not_found"}
    if not target_file.is_file():
        return {"_error": "not_a_file"}
    stat = target_file.stat()
    newlines = 0
    with target_file.open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            newlines += chunk.count(b"\n")
        total_lines = newlines + 1
        pos = fh.tell()
        buf = b""
        while pos > 0 and buf.count(b"\n") < tail_lines:
            step = min(chunk_size, pos)
            pos -= step
            fh.seek(pos)
            buf = fh.read(step) + buf
    parts = buf.split(b"\n")
    truncated = total_lines > tail_lines
    if truncated:
        # The leading part may start mid-character at a chunk boundary; it is
        # never part of the tail, so decode only what is returned.
        parts = parts[-tail_lines:]
    # faulthandler writes a thread name as raw bytes, which Python cuts to
    # Linux's 15-byte limit, so it can end mid-character; escape such bytes
    # rather than fail the whole read.
    content = b"\n".join(parts).decode("utf-8", errors="backslashreplace")
    return {
        "content": content,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "total_lines": total_lines,
        "truncated": truncated,
    }
