"""On-disk persistence for the settings UI.

File I/O for the three JSON files the settings UI owns in the data dir:

- ``tool_config.json`` — per-tool enabled/disabled/pinned state
  (``load_tool_config`` / ``save_tool_config``), plus the env-var overlay
  (``env_pinned_tools`` / ``effective_tool_config``).
- ``tool_metadata.json`` — the sidecar's lightweight tool-list cache
  (``dump_tool_metadata_cache`` / ``load_tool_metadata_cache``).
- ``backup_settings.json`` — the standalone-mode auto-backup override
  (``_load_backup_settings_override`` / ``_save_backup_settings_override``).

``_atomic_write_json`` is the shared tmp-then-rename writer, and
``_get_override_file_lock`` serialises the feature-flag/advanced-settings
read-modify-write. Leaf module (no imports from the settings_ui package)
so the handler families and ``__init__`` can depend on it without cycles.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import get_global_settings
from ..utils.data_paths import get_data_dir

if TYPE_CHECKING:
    from ..config import Settings

logger = logging.getLogger(__name__)


def _get_config_path() -> Path:
    """Return the path to the tool config JSON file.

    Delegates directory resolution to :func:`utils.data_paths.get_data_dir`,
    which handles ``HA_MCP_CONFIG_DIR`` override, add-on ``/data``,
    home-dir, and tmpdir fallback (memoized).
    """
    return get_data_dir() / "tool_config.json"


def _get_tool_metadata_cache_path() -> Path:
    """Return the path to the cached tool metadata JSON file.

    The stdio settings sidecar (a separate process spawned by stdio mode)
    reads this cache instead of constructing a full FastMCP server, so it
    stays lightweight. Refreshed by ``dump_tool_metadata_cache()`` on
    every parent stdio startup.
    """
    return get_data_dir() / "tool_metadata.json"


def dump_tool_metadata_cache(metadata: list[dict[str, Any]]) -> bool:
    """Persist tool metadata to disk for the sidecar to consume.

    Returns True on success, False on any OSError. Failures are logged
    but non-fatal — the sidecar will fall back to an empty list and the
    UI will show "no tools" rather than blocking startup of the MCP
    server itself.
    """
    path = _get_tool_metadata_cache_path()
    try:
        path.write_text(json.dumps(metadata))
    except OSError:
        logger.warning("Failed to dump tool metadata cache to %s", path, exc_info=True)
        return False
    return True


def load_tool_metadata_cache() -> list[dict[str, Any]]:
    """Read cached tool metadata from disk.

    Returns an empty list if the cache is missing, unreadable, or
    malformed — the sidecar must still serve the settings page (even
    with no tools listed) so the user can disable the sidecar itself
    via the ``HA_MCP_DISABLE_SETTINGS_UI`` toggle.
    """
    path = _get_tool_metadata_cache_path()
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return []
    except OSError:
        logger.warning("Cannot read tool metadata cache at %s", path, exc_info=True)
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # exc_info preserves the line / column of the failure so a
        # truncated-write looks distinguishable from corrupted-mid-file
        # in the logs.
        logger.warning(
            "Tool metadata cache at %s is not valid JSON", path, exc_info=True
        )
        return []
    if not isinstance(data, list):
        return []
    return data


def _seed_tool_config_from_env(settings: Settings) -> dict[str, str]:
    """Build the initial per-tool state map from the DISABLED_TOOLS /
    PINNED_TOOLS env vars.

    PINNED_TOOLS wins ties only where a tool is not already marked
    disabled (matches the historical seed semantics). Returns an empty
    dict when neither env var names anything.
    """
    tools: dict[str, str] = {}
    disabled_raw = getattr(settings, "disabled_tools", "")
    if disabled_raw:
        for name in disabled_raw.split(","):
            name = name.strip()
            if name:
                tools[name] = "disabled"
    pinned_raw = getattr(settings, "pinned_tools", "")
    if pinned_raw:
        for name in pinned_raw.split(","):
            name = name.strip()
            if name and name not in tools:
                tools[name] = "pinned"
    return tools


def load_tool_config(settings: Settings | None = None) -> dict[str, Any]:
    """Load persisted tool config, seeding from env vars if no file exists."""
    path = _get_config_path()
    # ``Path.exists()`` only swallows ``ENOENT/ENOTDIR/EBADF/ELOOP``; an
    # ``EACCES`` (e.g. ``HA_MCP_CONFIG_DIR`` pointing at a dir that exists
    # but isn't readable by the runtime UID) propagates. Read directly and
    # treat ``FileNotFoundError`` as "no config yet"; log other ``OSError``s.
    try:
        raw = path.read_text()
    except FileNotFoundError:
        raw = None
    except OSError:
        logger.warning("Cannot read tool config at %s", path, exc_info=True)
        raw = None

    if raw is not None:
        try:
            result: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Tool config at %s is not valid JSON; ignoring.", path)
        else:
            return result

    if settings is None:
        return {}

    # Seed from DISABLED_TOOLS / PINNED_TOOLS env vars
    tools = _seed_tool_config_from_env(settings)
    if tools:
        config = {"tools": tools}
        save_tool_config(config)
        logger.info("Seeded tool config from env vars (%d entries)", len(tools))
        return config
    return {}


def env_pinned_tools(settings: Settings | None = None) -> dict[str, str]:
    """Return {tool_name: "disabled" | "pinned"} for every tool named
    in the DISABLED_TOOLS or PINNED_TOOLS env vars.

    Used by the UI to render env-pinned rows as read-only and by the
    save handler to reject flips. PINNED_TOOLS wins ties (matches the
    existing seed semantics in load_tool_config).
    """
    if settings is None:
        settings = get_global_settings()
    pinned: dict[str, str] = {}
    for name in (settings.disabled_tools or "").split(","):
        name = name.strip()
        if name:
            pinned[name] = "disabled"
    for name in (settings.pinned_tools or "").split(","):
        name = name.strip()
        if name:
            pinned[name] = "pinned"
    return pinned


def effective_tool_config(settings: Settings | None = None) -> dict[str, Any]:
    """Return the runtime tool config: file values overlaid by env-
    pinned tools (the latter always win, never overwritten by file).

    Use this for any "what is the runtime state?" computation. Keep
    ``load_tool_config`` as the pure file reader (no env overlay) for
    cases that need just the file's contents (e.g. for displaying
    "user-set" status separately from "env-pinned" status).
    """
    if settings is None:
        settings = get_global_settings()
    cfg = load_tool_config(settings)
    tools = {**cfg.get("tools", {}), **env_pinned_tools(settings)}
    return {**cfg, "tools": tools}


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` atomically.

    Writes to ``<path>.tmp`` first and ``os.replace``s into place so a
    crash or out-of-space mid-write cannot leave a partial/empty file —
    callers that read the file back (``load_tool_config`` /
    ``_load_backup_settings_override``) would otherwise treat a
    half-written file as "no config" and silently fall back to defaults
    (e.g. re-enabling auto-backup or re-enabling tools the user
    disabled). Mirrors the pattern ``BackupManager._write_snapshot``
    already uses for snapshot files.

    Raises OSError on filesystem failure — same surface as the previous
    ``path.write_text`` so caller try/except shapes don't need updating.
    On failure, the ``.tmp`` file is cleaned up so a previous partial
    write does not accumulate next to the real file.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(str(tmp), str(path))
    except OSError:
        with contextlib.suppress(FileNotFoundError, OSError):
            tmp.unlink()
        raise


def save_tool_config(config: dict[str, Any]) -> bool:
    """Persist tool config to disk.

    Returns True on success, False on failure (read-only filesystem,
    permission denied, etc.). Caller is responsible for surfacing the
    failure to the user — the HTTP route at ``_save_tools`` returns 500
    so the UI's ``saveConfig`` shows "Save failed!" instead of the
    misleading "Saved — restart required".
    """
    path = _get_config_path()
    try:
        _atomic_write_json(path, config)
    except OSError:
        logger.exception("Failed to save tool config to %s", path)
        return False
    logger.info("Saved tool config to %s", path)
    return True


def _get_backup_settings_override_path() -> Path:
    """Return path to the auto-backup settings override file.

    Sits next to ``tool_config.json`` in the same data dir. Web UI edits
    in standalone (non-addon) mode persist here; ``get_global_settings``
    reads the file on the next call after ``_reset_global_settings``.
    """
    return get_data_dir() / "backup_settings.json"


def _load_backup_settings_override() -> dict[str, Any]:
    """Read the auto-backup override file, returning {} when absent/corrupt.

    Best-effort by design: a corrupt file logs a warning and is treated
    as empty so the Settings code path never breaks because of a
    malformed UI write.
    """
    path = _get_backup_settings_override_path()
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError:
        logger.warning(
            "Cannot read backup settings override at %s", path, exc_info=True
        )
        return {}
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "Backup settings override at %s is not valid JSON; ignoring.", path
        )
        return {}
    return data if isinstance(data, dict) else {}


def _save_backup_settings_override(data: dict[str, Any]) -> bool:
    """Persist the auto-backup override file. Returns True on success.

    Uses ``_atomic_write_json`` so a crash mid-write leaves the previous
    file (or no file) intact rather than a half-written one — the latter
    would parse as JSON-decode-fail in ``_load_backup_settings_override``
    and silently restore defaults (re-enabling auto-backup when the user
    had opted out).
    """
    path = _get_backup_settings_override_path()
    try:
        _atomic_write_json(path, data)
    except OSError:
        logger.exception("Failed to save backup settings override to %s", path)
        return False
    return True


# Serialises read-modify-write on the shared override file
# (``feature_flags.json``) so two concurrent saves can't interleave
# their reads and clobber each other's persisted state. Both
# ``_save_feature_flags`` and ``_save_advanced_settings``
# touch the same file; without this lock, request A reading before
# request B's ``os.replace`` lands would write back a merged dict that
# misses B's changes. The runtime master gate kept functionality
# correct even when this raced, but the persisted state lied about the
# user's intent — surfacing as "I set the flag and it came back off"
# after a restart.
_OVERRIDE_FILE_LOCK: asyncio.Lock | None = None


def _get_override_file_lock() -> asyncio.Lock:
    """Lazy lock construction. ``asyncio.Lock()`` on Python 3.10+ no
    longer takes a ``loop=`` argument and only binds to a loop on
    first ``acquire()`` via ``asyncio.get_event_loop()``. Either eager
    or lazy module-level construction works for this project's
    single-uvicorn-loop deployment; we keep the lazy pattern so a
    future test fixture that spins up its own loop doesn't lock in
    the import-time loop. Assumes a single asyncio event loop for the
    process lifetime — a multi-loop deployment (e.g. threaded handler
    dispatch) would race here.
    """
    global _OVERRIDE_FILE_LOCK
    if _OVERRIDE_FILE_LOCK is None:
        _OVERRIDE_FILE_LOCK = asyncio.Lock()
    return _OVERRIDE_FILE_LOCK
