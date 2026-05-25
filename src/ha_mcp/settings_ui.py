"""Web-based settings UI for tool visibility configuration.

Serves a self-contained HTML page at /settings that lets users enable,
disable, and pin MCP tools. Changes apply immediately without server
restart. Persists to a JSON config file alongside the MCP server data.

Works across all installation methods (add-on, Docker, standalone).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, NotRequired, TypedDict

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from ._version import is_running_in_addon
from .backup_manager import get_backup_manager
from .client.supervisor_client import make_supervisor_httpx_client
from .config import (
    BACKUP_OVERRIDE_FIELDS,
    _reset_global_settings,
    get_backup_setting_origin,
    get_global_settings,
)
from .errors import ErrorCode, create_error_response
from .transforms import DEFAULT_PINNED_TOOLS
from .utils.data_paths import get_data_dir

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from .config import Settings
    from .server import HomeAssistantSmartMCPServer


class ToolStub(TypedDict):
    """Metadata advertised in the settings UI for a tool that isn't visible
    in ``local_provider._list_tools()``.

    Two reasons a tool needs a stub: it's added by a FastMCP transform at
    runtime (``TRANSFORM_GENERATED_TOOLS``), or it's feature-gated and
    only registers when a setting is on (``FEATURE_GATED_TOOLS``). The
    consumer (`_get_tool_metadata`) renders the same shape for both;
    ``disabled_by`` is the only field that differs and signals UI
    placement of the "Beta — set X" hint.
    """

    title: str
    primary_tag: str
    description: str
    readOnlyHint: NotRequired[bool]
    destructiveHint: NotRequired[bool]
    disabled_by: NotRequired[str]


_VALID_STATES = frozenset({"enabled", "disabled", "pinned"})

# Per-process identity surfaced via ``/api/settings/info`` so the
# restart UI can tell whether the addon actually restarted (vs. the
# poll-cycle succeeding against the still-running OLD instance because
# the supervisor restart silently no-op'd). Generated once at module
# import; a fresh Python process gets a fresh value, so any restart
# that actually swaps processes flips both. ``started_at`` is Unix
# epoch seconds for human debuggability; ``instance_id`` is the
# load-bearing identifier the JS poll compares against.
_PROCESS_INSTANCE_ID: str = uuid.uuid4().hex
_PROCESS_STARTED_AT: float = time.time()

logger = logging.getLogger(__name__)

# Tools that are always enabled regardless of saved config — the server
# strips them out of any disable list before applying. Three of these
# overlap with DEFAULT_PINNED_TOOLS in transforms/categorized_search.py
# (ha_search_entities, ha_get_overview, ha_report_issue); ha_get_state
# is mandatory but not pinned-by-default because it is reachable via the
# ha_call_read_tool proxy when tool search is on. Keep these lists in
# sync where it matters and divergent where it matters — don't merge them.
MANDATORY_TOOLS: set[str] = {
    "ha_search_entities",
    "ha_get_overview",
    "ha_get_state",
    "ha_report_issue",
    # Skill guide carries the bundled best-practices trigger conditions
    # in its description — tool-only clients (claude.ai, etc.) rely on
    # seeing it in the catalog. Disabling it would silently break the
    # "consult skill before writing config" workflow.
    "ha_get_skill_guide",
    # Backups are operational essentials — needed as the pre-change safety
    # net before config edits and as the recovery path after them. Kept
    # always-on so users who aggressively disable everything keep a
    # working backup tool.
    "ha_manage_backup",
}

# Tools created by FastMCP transforms (not registered through
# local_provider). No transform-generated tools are currently in use —
# ``ha_get_skill_guide`` is registered the normal way and is visible
# through ``local_provider._list_tools()``. Kept as an empty dict so
# UI rendering, type contracts, and tests don't need to special-case
# the "no transform tools" path; populate when a future transform
# appends tools that need settings-UI visibility.
TRANSFORM_GENERATED_TOOLS: dict[str, ToolStub] = {}

# Tools that exist in the codebase but are only registered when a
# corresponding feature flag/env var is set. When the flag is off, these
# won't appear in local_provider._list_tools(), so we inject stub entries
# into the settings UI so users discover the tool exists and how to enable
# it. Keep this dict in sync with the ``"beta"`` tag added to each tool's
# source file (tools_yaml_config.py, tools_filesystem.py, tools_mcp_component.py,
# tools_code.py) — a future rename or removal needs to land in both places.
FEATURE_GATED_TOOLS: dict[str, ToolStub] = {
    "ha_config_set_yaml": {
        "title": "Set YAML Config",
        "primary_tag": "System",
        "description": "Add, replace, or remove top-level keys in configuration.yaml or package files.",
        "disabled_by": "enable_yaml_config_editing",
        "destructiveHint": True,
    },
    "ha_manage_custom_tool": {
        "title": "Custom Tool",
        "primary_tag": "System",
        "description": "Create and run a custom tool in a sandbox, or manage saved custom tools (code mode).",
        "disabled_by": "enable_code_mode",
        "destructiveHint": True,
    },
    "ha_list_files": {
        "title": "List Files",
        "primary_tag": "Files",
        "description": "List files in a directory within the Home Assistant config.",
        "disabled_by": "enable_filesystem_tools",
        "readOnlyHint": True,
    },
    "ha_read_file": {
        "title": "Read File",
        "primary_tag": "Files",
        "description": "Read a file from the Home Assistant config directory.",
        "disabled_by": "enable_filesystem_tools",
        "readOnlyHint": True,
    },
    "ha_write_file": {
        "title": "Write File",
        "primary_tag": "Files",
        "description": "Write a file to allowed directories in the Home Assistant config.",
        "disabled_by": "enable_filesystem_tools",
        "destructiveHint": True,
    },
    "ha_delete_file": {
        "title": "Delete File",
        "primary_tag": "Files",
        "description": "Delete a file from allowed directories.",
        "disabled_by": "enable_filesystem_tools",
        "destructiveHint": True,
    },
    "ha_install_mcp_tools": {
        "title": "Install MCP Tools Component",
        "primary_tag": "Utilities",
        "description": "Install the ha_mcp_tools custom component via HACS.",
        "disabled_by": "enable_custom_component_integration",
        "destructiveHint": True,
    },
}


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


def _render_stub(name: str, meta: ToolStub) -> dict[str, Any]:
    """Render a ToolStub as the dict shape ``_get_tool_metadata`` returns.

    Both transform-generated and feature-gated stubs share the same UI
    representation; the only meaningful difference is whether
    ``disabled_by`` carries the safety-toggle name (which the JS
    template renders as a "Beta — set X" hint). Annotations come
    through as bools and are dropped from the final dict when False
    so the JSON payload stays small.
    """
    annotations: dict[str, bool] = {}
    if meta.get("readOnlyHint"):
        annotations["readOnlyHint"] = True
    if meta.get("destructiveHint"):
        annotations["destructiveHint"] = True

    rendered: dict[str, Any] = {
        "name": name,
        "title": meta["title"],
        "description": meta["description"],
        "tags": [meta["primary_tag"]],
        "primary_tag": meta["primary_tag"],
        "annotations": annotations,
    }
    if "disabled_by" in meta:
        rendered["disabled_by"] = meta["disabled_by"]
    return rendered


async def _get_tool_metadata(
    server: HomeAssistantSmartMCPServer,
) -> list[dict[str, Any]]:
    """Extract metadata for all registered tools from the server.

    Uses FastMCP's internal ``local_provider._list_tools()`` because the
    public ``mcp.list_tools()`` filters out tools marked as disabled via
    ``mcp.disable()``. The settings UI specifically needs the UNFILTERED
    list so that users can see and re-enable tools they previously
    disabled. There is no public FastMCP API that returns the unfiltered
    list as of v3.2.0.
    """
    tools: list[dict[str, Any]] = []
    # Groups not considered "primary" when choosing a tool's canonical group —
    # these are cross-cutting tags (e.g. Z-Wave, Zigbee) that should not
    # override the tool's real domain group.
    secondary_tags = {"Z-Wave", "Zigbee"}

    registered = await server.mcp.local_provider._list_tools()
    for tool in registered:
        tags = sorted(tool.tags) if tool.tags else []
        primary_tags = [t for t in tags if t not in secondary_tags]
        primary = primary_tags[0] if primary_tags else (tags[0] if tags else "Other")
        annotations: dict[str, bool] = {}
        if tool.annotations:
            if getattr(tool.annotations, "readOnlyHint", None):
                annotations["readOnlyHint"] = True
            if getattr(tool.annotations, "destructiveHint", None):
                annotations["destructiveHint"] = True
        title = getattr(tool, "title", None) or tool.name
        if tool.annotations and getattr(tool.annotations, "title", None):
            title = tool.annotations.title
        tools.append(
            {
                "name": tool.name,
                "title": title,
                "description": (tool.description or "")[:200],
                "tags": tags,
                "primary_tag": primary,
                "annotations": annotations,
            }
        )

    registered_names = {t["name"] for t in tools}

    # Inject stub entries for tools generated by FastMCP transforms — these
    # never reach local_provider so they have to be advertised explicitly.
    for name, transform_meta in TRANSFORM_GENERATED_TOOLS.items():
        if name in registered_names:
            continue
        tools.append(_render_stub(name, transform_meta))
        registered_names.add(name)

    # Inject stub entries for feature-gated tools that aren't registered
    for name, meta in FEATURE_GATED_TOOLS.items():
        if name in registered_names:
            continue
        tools.append(_render_stub(name, meta))

    tools.sort(key=lambda t: (t["primary_tag"], t["name"]))
    return tools


class UserToolStateOverrides(NamedTuple):
    """User-explicit per-tool state overrides loaded from tool_config.json.

    Both sets are immutable frozensets so callers can't pollute the
    return value. They are disjoint by construction (a tool_config entry
    has one state per tool).

    - ``pinned_names``: tools the user explicitly set to "pinned"
    - ``enabled_names``: tools the user explicitly set to "enabled"
      (used by _apply_tool_search to unpin defaults the user re-enabled)
    """

    pinned_names: frozenset[str]
    enabled_names: frozenset[str]


def apply_tool_visibility(
    mcp: FastMCP,
    config: dict[str, Any],
    settings: Settings,
) -> UserToolStateOverrides:
    """Apply tool visibility from config, respecting safety toggles.

    Args:
        mcp: The FastMCP instance to enable/disable tools on.
        config: The tool_config.json contents (per-tool states).
        settings: The server Settings (for enable_yaml_config_editing etc.).

    Returns:
        A :class:`UserToolStateOverrides` carrying the user-pinned tools
        and the user-explicitly-enabled tools. The caller (server.py)
        uses ``enabled_names`` to filter ``DEFAULT_PINNED_TOOLS`` so a
        user can unpin a default by flipping it to "enabled" in the UI.
    """
    disabled_names: set[str] = set()
    pinned_names: set[str] = set()
    enabled_names: set[str] = set()

    tool_states = config.get("tools", {})
    for name, state in tool_states.items():
        if state == "disabled":
            disabled_names.add(name)
        elif state == "pinned":
            pinned_names.add(name)
        elif state == "enabled":
            enabled_names.add(name)

    # AND semantics for the YAML safety toggle: the tool is disabled if
    # *either* the safety toggle is off *or* the user disabled it in the UI.
    # Kept as defense-in-depth even though tools_yaml_config.py already
    # early-returns when the toggle is off (the tool isn't registered, so
    # mcp.disable() is a no-op in that case) — if the registration site
    # ever moves, this still keeps the tool out of the visible catalog.
    if not settings.enable_yaml_config_editing:
        disabled_names.add("ha_config_set_yaml")

    disabled_names -= MANDATORY_TOOLS

    if disabled_names:
        mcp.disable(names=disabled_names)
        logger.info("Disabled tools: %s", ", ".join(sorted(disabled_names)))

    mcp.enable(names=MANDATORY_TOOLS)

    assert pinned_names.isdisjoint(enabled_names), (
        "pinned and enabled overrides must be disjoint by construction"
    )

    return UserToolStateOverrides(
        pinned_names=frozenset(pinned_names),
        enabled_names=frozenset(enabled_names),
    )


_SETTINGS_HTML = (
    """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>HA-MCP Tool Settings</title>
<style>
  :root {
    --bg: #1c1c1e; --surface: #2c2c2e; --surface-hover: #3a3a3c;
    --text: #f5f5f7; --text-secondary: #98989d; --accent: #0a84ff;
    --accent-hover: #409cff; --danger: #ff453a; --success: #30d158;
    --warning: #ffd60a; --border: #38383a; --disabled-bg: #1a1a1c;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.5; padding: 16px; }
  .header { display: flex; align-items: center; justify-content: space-between;
    padding: 16px 0; border-bottom: 1px solid var(--border); margin-bottom: 16px; }
  .header h1 { font-size: 1.5rem; font-weight: 600; }
  .status { font-size: 0.85rem; padding: 4px 12px; border-radius: 12px;
    background: var(--surface); color: var(--text-secondary); }
  .status.saved { background: #0d3b1e; color: var(--success); }
  .search { width: 100%; padding: 10px 16px; border-radius: 10px; border: 1px solid var(--border);
    background: var(--surface); color: var(--text); font-size: 0.95rem; margin-bottom: 16px;
    outline: none; }
  .search:focus { border-color: var(--accent); }
  .readonly-notice { background: #1a2a3a; border: 1px solid #1a4a7a; border-radius: 10px;
    padding: 12px 16px; margin-bottom: 16px; font-size: 0.85rem; color: #6cb4ff; }
  .group { background: var(--surface); border-radius: 12px; margin-bottom: 8px;
    overflow: hidden; border: 1px solid var(--border); }
  .group-header { display: flex; align-items: center; justify-content: space-between;
    padding: 12px 16px; cursor: pointer; user-select: none; gap: 12px; }
  .group-header:hover { background: var(--surface-hover); }
  .group-header-left { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0; }
  .group-name { font-weight: 600; font-size: 0.95rem; }
  .group-count { font-size: 0.8rem; color: var(--text-secondary); }
  .group-chevron { transition: transform 0.2s; color: var(--text-secondary);
    display: inline-block; width: 12px; }
  .group-chevron.open { transform: rotate(90deg); }
  .group-master { flex-shrink: 0; }
  .group-tools { display: none; border-top: 1px solid var(--border); }
  .group-tools.open { display: block; }
  .tool { display: flex; align-items: center; justify-content: space-between;
    padding: 10px 16px; border-bottom: 1px solid var(--border); }
  .tool:last-child { border-bottom: none; }
  .tool.hidden { display: none; }
  .tool.env-pinned .tool-name { color: var(--text-secondary); }
  .tool-info { flex: 1; min-width: 0; }
  .tool-name { font-size: 0.9rem; font-weight: 500; }
  .tool-meta { font-size: 0.75rem; color: var(--text-secondary); margin-top: 2px; }
  .tool-desc { font-size: 0.8rem; color: var(--text-secondary); margin-top: 2px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .badge { display: inline-block; font-size: 0.7rem; padding: 1px 6px;
    border-radius: 4px; margin-left: 6px; font-weight: 500; }
  .badge.readonly { background: #1a2a3a; color: #6cb4ff; }
  .badge.destructive { background: #3a1a1a; color: #ff6b6b; }
  .badge.mandatory { background: #1a3a1a; color: #6bff6b; }
  .tool-toggles { display: flex; gap: 16px; align-items: center; }
  .toggle-group { display: flex; flex-direction: column; align-items: center; gap: 2px;
    font-size: 0.7rem; color: var(--text-secondary); }
  .toggle-group.disabled-toggle { opacity: 0.35; }
  .switch { position: relative; display: inline-block; width: 36px; height: 20px; }
  .switch input { opacity: 0; width: 0; height: 0; }
  .slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0;
    background: #555; border-radius: 10px; transition: background 0.2s; }
  .slider::before { position: absolute; content: ""; height: 14px; width: 14px; left: 3px;
    top: 3px; background: var(--text); border-radius: 50%; transition: transform 0.2s; }
  input:checked + .slider { background: var(--accent); }
  input:checked + .slider::before { transform: translateX(16px); }
  input:disabled + .slider { cursor: not-allowed; opacity: 0.4; }
  .disabled-by-note { font-size: 0.7rem; color: var(--warning); margin-top: 2px;
    font-style: italic; }
  .summary { display: flex; gap: 16px; padding: 8px 0; margin-bottom: 16px;
    font-size: 0.85rem; color: var(--text-secondary); flex-wrap: wrap; }
  .summary span { background: var(--surface); padding: 4px 12px; border-radius: 8px; }
  .pin-notice { background: #3a2e1a; border: 1px solid #7a5a1a; border-radius: 10px;
    padding: 10px 16px; margin-bottom: 12px; font-size: 0.85rem; color: #ffd680; display: none; }
  .pin-notice.show { display: block; }
  .restart-notice { background: #3a1a1a; border: 1px solid #7a1a1a; border-radius: 10px;
    padding: 12px 16px; margin-bottom: 12px; font-size: 0.9rem; color: #ff9090;
    font-weight: 500; display: none; align-items: center; justify-content: space-between; gap: 12px; }
  .restart-notice.show { display: flex; }
  .restart-notice-text { flex: 1; }
  .restart-btn { padding: 8px 16px; border-radius: 8px; border: none;
    background: var(--accent); color: white; font-weight: 600; cursor: pointer;
    font-size: 0.85rem; flex-shrink: 0; }
  .restart-btn:hover { background: var(--accent-hover); }
  .restart-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  /* Destructive variant — visually distinct from the primary accent
     so a glance at the page makes "Disable settings server"
     obviously not a routine click. Matches the restart-notice red
     family so the danger semantic reads even without label text. */
  .danger-btn { padding: 7px 14px; border-radius: 8px;
    border: 1px solid #7a1a1a; background: transparent; color: #ff9090;
    font-weight: 600; cursor: pointer; font-size: 0.8rem; flex-shrink: 0; }
  .danger-btn:hover { background: #2a0e0e; }
  .danger-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  /* Tabs — generic structure other tabs can stack onto without
     touching existing markup. New tabs add a button to .tabs and
     a sibling .panel below; the JS switcher dispatches via the
     data-panel attribute. */
  .tabs { display: flex; gap: 4px; margin-bottom: 16px; border-bottom: 1px solid var(--border); }
  .tab { padding: 10px 16px; border: none; background: transparent; color: var(--text-secondary);
    font-size: 0.95rem; cursor: pointer; border-bottom: 2px solid transparent; font-weight: 500; }
  .tab.active { color: var(--text); border-bottom-color: var(--accent); }
  .panel { display: none; }
  .panel.active { display: block; }
  /* Backups */
  .backup-filters { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
  .backup-filters input { padding: 8px 12px; border-radius: 8px; border: 1px solid var(--border);
    background: var(--surface); color: var(--text); font-size: 0.85rem; }
  .backup-filters input:focus { border-color: var(--accent); outline: none; }
  .backup-filters button { padding: 8px 12px; border-radius: 8px; border: none;
    background: var(--surface); color: var(--text); font-size: 0.85rem; cursor: pointer; }
  .backup-filters button:hover { background: var(--surface-hover); }
  .backup-filters button.danger { background: #3a1a1a; color: #ff6b6b; }
  .backup-state { background: var(--surface); border-radius: 10px; padding: 10px 16px; margin-bottom: 12px;
    font-size: 0.85rem; color: var(--text-secondary); display: flex; gap: 16px; flex-wrap: wrap; }
  .backup-state span { display: inline-block; }
  .backup-state strong { color: var(--text); }
  .backup-row { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    padding: 10px 14px; margin-bottom: 6px; display: flex; align-items: center; gap: 12px; }
  .backup-row-info { flex: 1; min-width: 0; }
  .backup-row-name { font-size: 0.9rem; font-weight: 500; word-break: break-all; }
  .backup-row-meta { font-size: 0.75rem; color: var(--text-secondary); margin-top: 2px; }
  .backup-row-actions { display: flex; gap: 6px; flex-shrink: 0; }
  .backup-row-actions button { padding: 6px 10px; border-radius: 6px; border: none;
    background: var(--accent); color: white; font-size: 0.8rem; cursor: pointer; }
  .backup-row-actions button:hover { background: var(--accent-hover); }
  .backup-row-actions button.danger { background: var(--danger); }
  .backup-row-actions button.secondary { background: var(--surface-hover); color: var(--text); }
  .backup-empty { padding: 24px; text-align: center; color: var(--text-secondary); font-size: 0.9rem;
    background: var(--surface); border: 1px dashed var(--border); border-radius: 10px; }
  .backup-config { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    padding: 14px 16px; margin-bottom: 12px; }
  .backup-config-form { display: grid; grid-template-columns: 1fr; gap: 10px; }
  .backup-field { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .backup-field-label { min-width: 200px; font-size: 0.9rem; font-weight: 500; }
  .backup-field-control input[type="number"] { width: 120px; padding: 6px 10px;
    border-radius: 6px; border: 1px solid var(--border); background: var(--bg); color: var(--text); }
  .backup-field-control input[type="number"]:disabled { opacity: 0.55; cursor: not-allowed; }
  .backup-field-locked { background: #2a2520; color: #f4b860; font-size: 0.78rem;
    padding: 2px 8px; border-radius: 999px; }
  .backup-field-help { font-size: 0.75rem; color: var(--text-secondary); flex-basis: 100%; margin-left: 200px; }
  .backup-config-actions { display: flex; align-items: center; gap: 12px; margin-top: 10px;
    padding-top: 10px; border-top: 1px solid var(--border); }
  .backup-config-actions button { padding: 8px 16px; border-radius: 6px; border: none;
    background: var(--accent); color: white; font-size: 0.9rem; cursor: pointer; font-weight: 500; }
  .backup-config-actions button:hover { background: var(--accent-hover); }
  .backup-config-actions button:disabled { opacity: 0.5; cursor: not-allowed; }
  /* Modal */
  .modal-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.7);
    display: none; align-items: center; justify-content: center; z-index: 10; padding: 16px; }
  .modal-backdrop.show { display: flex; }
  .modal { background: var(--bg); border: 1px solid var(--border); border-radius: 12px;
    max-width: 900px; width: 100%; max-height: 90vh; display: flex; flex-direction: column; }
  .modal-header { padding: 14px 16px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  .modal-title { font-size: 1.05rem; font-weight: 600; word-break: break-all; }
  .modal-close { background: transparent; border: none; color: var(--text-secondary);
    font-size: 1.4rem; cursor: pointer; padding: 0 8px; }
  .modal-body { flex: 1; overflow: auto; padding: 16px; }
  .modal-body pre { background: var(--surface); padding: 12px; border-radius: 8px;
    font-size: 0.8rem; overflow: auto; line-height: 1.4; }
  .diff-add { color: #6bff6b; }
  .diff-rem { color: #ff6b6b; }
  .diff-hdr { color: #6cb4ff; }
  /* Server-settings rows (#863). One row per FEATURE_FLAG_FIELDS
     entry. Locked rows (env / addon origin) get the dim treatment +
     a small inline note pointing at the env var to adjust. */
  .features-sub { font-size: 0.75rem; color: var(--text-secondary);
    margin-bottom: 8px; }
  .feature-row { display: flex; align-items: flex-start; justify-content: space-between;
    gap: 12px; padding: 10px 0; border-top: 1px solid var(--border); }
  .feature-row:first-child { border-top: none; }
  .feature-info { flex: 1; min-width: 0; }
  .feature-name { font-size: 0.9rem; font-weight: 500; }
  .feature-help { font-size: 0.75rem; color: var(--text-secondary); margin-top: 2px;
    line-height: 1.4; }
  .feature-help code { background: #111; padding: 1px 5px; border-radius: 4px;
    font-size: 0.72rem; }
  .feature-locked-note { font-size: 0.72rem; color: var(--warning); margin-top: 4px;
    font-style: italic; }
  .feature-control { flex-shrink: 0; display: flex; align-items: center; }
  .feature-control input[type="number"] { width: 64px; padding: 4px 8px;
    background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
    color: var(--text); font-size: 0.85rem; }
  .feature-control input[type="number"]:disabled { opacity: 0.4; cursor: not-allowed; }
  .feature-row.locked .feature-name { color: var(--text-secondary); }
  /* Beta master toggle + nested sub-rows (#1164). The master row
     ``.beta-master-row`` is visually distinguished as a section
     header. The 5 sub-rows ``.beta-sub`` are indented with a vertical
     connector line on the left; when master is off they get
     ``.dimmed`` (reduced opacity + disabled inputs) to make the
     gated state visible without removing rows from the DOM. */
  .feature-row.beta-master-row { font-weight: 600; padding-top: 16px;
    border-top: 2px solid var(--border); margin-top: 8px; }
  .feature-row.beta-master-row .feature-help { font-weight: 400; }
  .feature-row.beta-sub { padding-left: 32px; position: relative; }
  .feature-row.beta-sub::before { content: ""; position: absolute;
    left: 12px; top: 0; bottom: 0; width: 2px; background: var(--border); }
  .feature-row.beta-sub.dimmed { opacity: 0.55; }
  .feature-row.beta-sub.dimmed input { cursor: not-allowed; }
  /* Code-mode sub-numerics — second-level nesting under the
     enable_code_mode beta sub-row (#1164 Chunk 3b). Same dimming
     logic as beta-sub but deeper-indented and gated by
     enable_code_mode rather than the master. */
  .feature-row.codemode-sub { padding-left: 56px; position: relative; }
  .feature-row.codemode-sub::before { content: ""; position: absolute;
    left: 36px; top: 0; bottom: 0; width: 2px; background: var(--border); }
  .feature-row.codemode-sub.dimmed { opacity: 0.55; }
  .feature-row.codemode-sub.dimmed input { cursor: not-allowed; }
  /* Advanced settings sections (#1164) — one row per
     ADVANCED_SETTINGS_FIELDS entry, grouped by section. Visually
     matches the .feature-row treatment so the Server Settings tab
     reads as one coherent surface. */
  .adv-section-title { font-size: 0.85rem; font-weight: 600;
    color: var(--text-secondary); margin: 20px 0 4px; text-transform: uppercase;
    letter-spacing: 0.05em; }
  /* Beta section header is visually distinct (warning color, slightly
     larger) so the dangerous-features block at the bottom is impossible
     to miss as a category boundary (#1164 follow-up). */
  .adv-section-title.beta-section-title { color: var(--warning);
    font-size: 0.95rem; margin-top: 32px;
    border-top: 1px solid var(--warning); padding-top: 16px; }
  /* Two-step save note + primary-CTA save button (#1164 follow-up).
     The default <button> in this UI is intentionally small/neutral
     because most surfaces have many of them; the Save row gets a
     dedicated, larger primary style so users don't miss the action.
     Duplicated at top and bottom so scrolling either way reaches it. */
  .adv-save-note { background: rgba(255, 152, 0, 0.08);
    border-left: 3px solid var(--warning); padding: 10px 14px;
    border-radius: 6px; margin: 12px 0; color: var(--text);
    font-size: 0.85rem; line-height: 1.4; }
  .adv-save-note strong { color: var(--warning); }
  .adv-save-row { display: flex; align-items: center; gap: 12px;
    margin: 16px 0; padding: 8px 0; }
  .adv-save-btn { padding: 10px 22px; font-size: 1rem; font-weight: 600;
    border: none; border-radius: 8px; background: var(--accent);
    color: white; cursor: pointer; box-shadow: 0 2px 6px rgba(0,0,0,0.15);
    transition: background 0.15s ease, transform 0.05s ease; }
  .adv-save-btn:hover { background: var(--accent-hover); }
  .adv-save-btn:active { transform: translateY(1px); }
  .adv-save-btn:disabled { opacity: 0.55; cursor: not-allowed;
    box-shadow: none; }
  .adv-section { border-top: 1px solid var(--border); }
  .adv-row { display: flex; align-items: flex-start; justify-content: space-between;
    gap: 12px; padding: 10px 0; border-top: 1px solid var(--border); }
  .adv-row:first-child { border-top: none; }
  .adv-row.locked .adv-name { color: var(--text-secondary); }
  .adv-info { flex: 1; min-width: 0; }
  .adv-name { font-size: 0.9rem; font-weight: 500; }
  .adv-help { font-size: 0.75rem; color: var(--text-secondary); margin-top: 2px;
    line-height: 1.4; }
  .adv-help code { background: #111; padding: 1px 5px; border-radius: 4px;
    font-size: 0.72rem; }
  .adv-locked-note { font-size: 0.72rem; color: var(--warning); margin-top: 4px;
    font-style: italic; }
  .adv-control { flex-shrink: 0; display: flex; align-items: center; }
  .adv-control input[type="text"],
  .adv-control input[type="number"],
  .adv-control select { padding: 4px 8px; background: var(--bg);
    border: 1px solid var(--border); border-radius: 6px; color: var(--text);
    font-size: 0.85rem; min-width: 120px; }
  .adv-control input:disabled,
  .adv-control select:disabled { opacity: 0.4; cursor: not-allowed; }
  .adv-control input[type="checkbox"]:disabled { opacity: 0.4; cursor: not-allowed; }
  /* Tool Security Policies — per-tool card layout.
     Cards reuse the surface/border variables already in use elsewhere
     so they read consistently with backup-row / group blocks. */
  .policy-rule-card { background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 12px 14px; margin: 8px 0; }
  .policy-rule-header { display: flex; justify-content: space-between;
    align-items: center; margin-bottom: 8px; }
  .policy-rule-header strong { font-size: 0.95rem; }
  .policy-rule-remove { background: transparent; border: none;
    color: var(--text-secondary); cursor: pointer; font-size: 1.1rem;
    padding: 0 6px; }
  .policy-rule-remove:hover { color: var(--danger); }
  .policy-predicate-list { list-style: none; padding: 0; margin: 6px 0; }
  .policy-predicate-row { padding: 4px 0; display: flex; align-items: center;
    gap: 8px; flex-wrap: wrap; }
  .policy-predicate-row code { background: var(--bg); padding: 3px 8px;
    border-radius: 4px; font-size: 0.8rem; color: var(--text); }
  .policy-predicate-row button { background: transparent; border: none;
    color: var(--accent); cursor: pointer; font-size: 0.8rem; padding: 2px 4px; }
  .policy-predicate-row button:hover { text-decoration: underline; }
  .policy-add-predicate { background: transparent; border: 1px dashed var(--border);
    color: var(--accent); padding: 6px 12px; border-radius: 6px;
    cursor: pointer; font-size: 0.85rem; margin-top: 4px; }
  .policy-add-predicate:hover { background: var(--surface-hover); }
  .policy-predicate-form { background: var(--bg); padding: 10px;
    margin: 6px 0; border: 1px dashed var(--border); border-radius: 6px;
    display: flex; flex-direction: column; gap: 8px; }
  .policy-predicate-form .policy-form-row { display: flex; flex-wrap: wrap;
    gap: 6px; align-items: center; }
  .policy-predicate-form .policy-form-label { min-width: 90px;
    color: var(--text-secondary); font-size: 0.82rem; }
  .policy-predicate-form select,
  .policy-predicate-form input { padding: 5px 8px; border-radius: 4px;
    border: 1px solid var(--border); background: var(--surface);
    color: var(--text); font-size: 0.85rem; }
  .policy-predicate-form input.policy-predicate-path-custom { min-width: 200px; }
  .policy-predicate-form input.policy-predicate-value { min-width: 220px;
    font-family: monospace; }
  .policy-predicate-form .policy-form-hint { font-size: 0.75rem;
    color: var(--text-secondary); padding-left: 96px; margin-top: -4px; }
  .policy-predicate-form-error { color: var(--danger); font-size: 0.78rem;
    width: 100%; margin-top: 4px; }
  .policy-rule-lifetime { margin: 10px 0; font-size: 0.85rem;
    color: var(--text-secondary); }
  .policy-rule-lifetime input { width: 64px; padding: 4px 8px;
    background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
    color: var(--text); font-size: 0.85rem; margin: 0 6px; }
  .policy-save-status { margin-left: 10px; font-size: 0.8rem;
    color: var(--text-secondary); }
</style>
</head>
<body>
<div class="header">
  <h1>HA-MCP Settings</h1>
  <span id="status" class="status">Loading...</span>
</div>
<div class="tabs">
  <button class="tab active" data-panel="tools">Tools</button>
  <button class="tab" data-panel="server">Server Settings</button>
  <button class="tab" data-panel="backups">Backups</button>
  <button class="tab" data-panel="tool-security-policies">Tool Security Policies</button>
</div>
<div class="restart-notice" id="restartNotice">
  <span class="restart-notice-text" id="restartNoticeText">
    ⚠ Changes saved. Restart ha-mcp for them to take effect — disabled
    tools will be fully removed from the MCP tool list on next startup.
  </span>
  <button class="restart-btn" id="restartBtn" style="display:none">Restart Add-on</button>
</div>
<div class="panel active" id="panel-tools">
  <div class="readonly-notice">
    Server-wide features (Tool Search, YAML config editing, filesystem
    tools, etc.) appear in both the <strong>Server Settings</strong>
    tab and the add-on Configuration page — they're the same settings
    either way. Either surface stays in sync with the other after the
    addon restart. Changes require an MCP-host restart to apply.
  </div>
  <div class="pin-notice show" id="pinNotice">
    Tools listed in the <code>DISABLED_TOOLS</code> or <code>PINNED_TOOLS</code>
    env vars are locked read-only — unset the env var to edit them here.
    Pin toggles only take effect when Tool Search is enabled (Server Settings tab
    or add-on Configuration page).
  </div>
  <div class="summary" id="summary"></div>
  <input type="text" class="search" id="search" placeholder="Search tools...">
  <div id="groups"></div>
</div>
<div class="panel" id="panel-server">
  <div class="features-sub">
    Tool Search, advanced settings. Changes require an MCP-host restart
    to take effect (close + reopen Claude Desktop, restart the add-on, etc.).
  </div>

  <!-- Two-step note + top Save button (#1164 follow-up). The Save +
       Restart workflow is non-obvious — users have hit the page,
       toggled, and then wondered why nothing took effect because they
       skipped one of the two steps. Display the note prominently and
       duplicate the Save button at the top so a user scrolling either
       end of the panel can hit it. -->
  <div class="adv-save-note">
    ⚠ Two-step save: <strong>(1) click "Save advanced settings"</strong>
    to persist your changes, then <strong>(2) click "Restart"</strong>
    above to apply them. Neither step alone is enough.
  </div>
  <div id="advSaveRowTop" class="adv-save-row" style="display:none;">
    <button id="advSaveBtnTop" class="adv-save-btn">💾 Save advanced settings</button>
    <span id="advSaveStatusTop" class="status"></span>
  </div>
  <div id="featuresBody"></div>

  <!-- Advanced settings sections (#1164). The "Connection
       (display only)" section was removed per user feedback — it
       just listed the read-only HOMEASSISTANT_URL / TOKEN /
       SUPERVISOR_TOKEN fields, which the user can already see in the
       addon's own logs and configuration. Registry entries for the
       connection section remain in ADVANCED_SETTINGS_FIELDS so the
       API still returns them (env-pin debugging, future surfaces),
       but they are not rendered into a panel here. -->
  <h3 class="adv-section-title">Search &amp; matching</h3>
  <div id="advSearch" class="adv-section"></div>
  <h3 class="adv-section-title">Operations</h3>
  <div id="advOperations" class="adv-section"></div>
  <h3 class="adv-section-title">Tool surface</h3>
  <div id="advToolsSurface" class="adv-section"></div>
  <h3 class="adv-section-title">Diagnostics</h3>
  <div id="advDiagnostics" class="adv-section"></div>

  <!-- Beta features moved to bottom of the panel (#1164 follow-up) —
       these can damage the HA system, so they sit last so the user
       sees safer settings first. -->
  <h3 class="adv-section-title beta-section-title">Beta features (dangerous)</h3>
  <div id="betaBody"></div>

  <!-- Bottom Save row sits AFTER the beta block (and any nested
       code-mode sub-numerics) so a user editing those doesn't have
       to scroll back up past their own changes (#1164 follow-up). -->
  <div class="adv-save-note">
    ⚠ Two-step save: <strong>(1) click "Save advanced settings"</strong>
    to persist your changes, then <strong>(2) click "Restart"</strong>
    above to apply them. Neither step alone is enough.
  </div>
  <div id="advSaveRow" class="adv-save-row" style="display:none;">
    <button id="advSaveBtn" class="adv-save-btn">💾 Save advanced settings</button>
    <span id="advSaveStatus" class="status"></span>
  </div>

  <div id="sidecarStopRow" style="display:none; margin: 16px 0; text-align: right;">
    <button class="danger-btn" id="stopSidecarBtn"
            title="Permanently disables the settings UI: stops this server AND writes ~/.ha-mcp/settings_ui_disabled so it does not respawn on future ha-mcp launches. Delete that file to re-enable."
    >Permanently disable settings server</button>
  </div>
</div>
<div class="panel" id="panel-backups">
  <div class="backup-state" id="backupState">Loading backup state…</div>
  <div class="backup-config" id="backupConfig">
    <div class="backup-config-form" id="backupConfigForm"></div>
    <div class="backup-config-actions" id="backupConfigActions" style="display:none">
      <button id="backupConfigSave">Save settings</button>
      <span id="backupConfigStatus" class="status"></span>
    </div>
  </div>
  <div class="backup-filters">
    <input type="text" id="backupDomain" placeholder="Domain (e.g. automation)">
    <input type="text" id="backupEntity" placeholder="Entity ID">
    <button id="backupRefresh">Refresh</button>
    <button id="backupBulkDelete" class="danger">Bulk delete matching…</button>
  </div>
  <div id="backupList"></div>
</div>
<div class="panel" id="panel-tool-security-policies">
  <h2>Tool Security Policies</h2>
  <p class="features-sub">
    Per-tool approval gating for high-stakes calls. Use the
    <strong>Tools</strong> tab to enable gating for a tool, then refine
    the matching conditions and approval lifetime here. Condition operators:
    equals, is one of, regex, contains, is present, greater than, less than.
  </p>

  <section id="policy-global-settings" style="margin-bottom:16px">
    <h3 style="font-size:1rem;margin-bottom:8px">Global settings</h3>
    <div class="feature-row">
      <div class="feature-info">
        <div class="feature-name">Enable Tool Security Policies</div>
        <div class="feature-help">
          Master switch. Mirrors the toggle in Server Settings. Off by
          default — toggle on and restart the addon to activate the
          gating middleware. While off, the rules below persist but
          aren't enforced.
        </div>
      </div>
      <div class="feature-control">
        <label class="switch">
          <input type="checkbox" id="policy-master-toggle">
          <span class="slider"></span>
        </label>
      </div>
    </div>
    <div class="feature-row">
      <div class="feature-info">
        <div class="feature-name">Wait seconds (5-600)</div>
        <div class="feature-help">How long the middleware waits for an approval before timing out.</div>
      </div>
      <div class="feature-control">
        <input type="number" id="policy-wait-seconds" min="5" max="600">
      </div>
    </div>
    <div class="feature-row">
      <div class="feature-info">
        <div class="feature-name">Approval TTL minutes (1-60)</div>
        <div class="feature-help">How long a pending approval stays in the queue before expiring.</div>
      </div>
      <div class="feature-control">
        <input type="number" id="policy-ttl-minutes" min="1" max="60">
      </div>
    </div>
    <div style="margin-top:10px; display:flex; align-items:center; gap:12px">
      <button id="policy-save-global-btn" class="restart-btn">Save global settings</button>
      <span id="policy-global-save-status" class="status"></span>
    </div>
  </section>

  <section id="policy-pending" style="margin-bottom:16px">
    <h3 style="font-size:1rem;margin-bottom:8px">Pending approvals</h3>
    <div id="policy-pending-list" class="backup-empty">No pending approvals.</div>
  </section>

  <section id="policy-rules">
    <h3 style="font-size:1rem;margin-bottom:8px">Gated tools</h3>
    <div id="policy-load-error" style="display:none;background:var(--danger);color:white;padding:8px 12px;border-radius:6px;margin-bottom:8px;font-size:0.85rem;"></div>
    <div id="policy-rules-empty" class="backup-empty" style="display:none;">
      No tools currently security-gated. Enable per-tool gating from the
      <a href="#" data-panel-link="tools">Tools</a> tab.
    </div>
    <div id="policy-rules-list"></div>
  </section>
</div>
<div class="modal-backdrop" id="modalBackdrop">
  <div class="modal">
    <div class="modal-header">
      <span class="modal-title" id="modalTitle"></span>
      <button class="modal-close" id="modalClose">×</button>
    </div>
    <div class="modal-body" id="modalBody"></div>
  </div>
</div>
<script>
// Catch top-level / async script errors and surface them in the
// status bar so a perpetually-"Loading" page becomes self-diagnosing
// (no devtools required). Without this, a script-evaluation error
// in any of the function definitions below would abort the script
// before loadTools() is even called, leaving the status stuck at
// the initial "Loading...".
window.addEventListener('error', (e) => {
  const el = document.getElementById('status');
  if (!el) return;
  const where = e.filename ? `${e.filename}:${e.lineno}:${e.colno}` : 'inline';
  el.textContent = `JS error: ${e.message} @ ${where}`;
});
window.addEventListener('unhandledrejection', (e) => {
  const el = document.getElementById('status');
  if (!el) return;
  el.textContent = `Async error: ${e.reason && e.reason.message ? e.reason.message : String(e.reason)}`;
});

let toolData = [];
let toolStates = {};
// Map of tool name → "disabled" | "pinned" for env-var-pinned tools.
// Populated from data.env_pinned in loadTools(); read by render() to
// lock rows and show the env-var name banner.
let toolEnvPinned = {};
let saveTimer = null;
let openGroups = new Set();

// Per-tool "security gated" toggle state mirrors policy.rules from
// /api/policy/config. A tool is gated iff there's any rule with a
// matching tool_name (with or without conditions). The Tools tab
// uses this set to render the third toggle alongside enabled/pinned.
// `enabled` is tri-state: true/false from the addon-config flag, or
// null when the features fetch failed — downstream branches need to
// distinguish "definitively off" from "couldn't determine" so they
// don't false-confidently tell the user the feature is off.
const policyState = {
  enabled: false,
  enabledKnown: false,
  gatedTools: new Set(),
};

async function loadPolicyState() {
  // policyState.enabled mirrors the addon-config flag
  // (enable_tool_security_policies) — the single source of truth for
  // whether the middleware is active. Read it from /api/settings/features
  // where it appears via FEATURE_FLAG_FIELDS.
  try {
    const fresp = await fetch('./api/settings/features');
    if (fresp.ok) {
      const fdata = await fresp.json();
      const flag = (fdata.flags || {})['enable_tool_security_policies'];
      policyState.enabled = !!(flag && flag.value);
      policyState.enabledKnown = true;
    } else {
      policyState.enabled = false;
      policyState.enabledKnown = false;
    }
  } catch (_e) {
    policyState.enabled = false;
    policyState.enabledKnown = false;
  }
  try {
    const r = await fetch('./api/policy/config');
    if (!r.ok) {
      policyState.gatedTools = new Set();
      return;
    }
    const p = await r.json();
    policyState.gatedTools = new Set((p.rules || []).map(rule => rule.tool_name));
  } catch (_e) {
    // Policy endpoint unavailable (sidecar stub) — leave gatedTools empty.
    policyState.gatedTools = new Set();
  }
}

// Wrap PUT /api/policy/config so every caller gets identical handling of
// the 409 (optimistic-concurrency) and other failure paths. The full
// policy round-trips through every caller, so the version GET'd here
// goes back out in the PUT body and the server can reject stale writes.
async function policyPut(policy, opLabel) {
  const w = await fetch('./api/policy/config', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(policy),
  });
  if (w.status === 409) {
    throw new Error(opLabel + ' failed: policy was modified in another tab/session. Reload the page, then re-apply your changes.');
  }
  if (!w.ok) throw new Error(opLabel + ' failed: ' + w.status + ' ' + await w.text());
  return await w.json();
}

async function syncPolicyRule(toolName, gated) {
  const r = await fetch('./api/policy/config');
  if (!r.ok) throw new Error('Could not load policy: ' + r.status);
  const policy = await r.json();
  policy.rules = policy.rules || [];
  if (gated) {
    if (!policy.rules.some(rule => rule.tool_name === toolName)) {
      policy.rules.push({tool_name: toolName, when: [], remember_minutes: 0});
    }
  } else {
    policy.rules = policy.rules.filter(rule => rule.tool_name !== toolName);
  }
  await policyPut(policy, 'Sync gated toggle');
}

async function loadTools() {
  let resp;
  try {
    resp = await fetch('./api/settings/tools');
  } catch (e) {
    updateStatus('Network error reaching /api/settings/tools: ' + e.message);
    return;
  }
  if (!resp.ok) {
    updateStatus(`/api/settings/tools returned HTTP ${resp.status} ${resp.statusText}`);
    return;
  }
  let data;
  try {
    data = await resp.json();
  } catch (e) {
    updateStatus('Failed to parse /api/settings/tools response as JSON: ' + e.message);
    return;
  }
  toolData = data.tools || [];
  toolStates = data.states || {};
  toolEnvPinned = data.env_pinned || {};
  // Load policy state before the first render so the "security gated"
  // toggle reflects current policy.rules. loadPolicyState() never throws
  // — it leaves gatedTools empty on failure.
  await loadPolicyState();
  if (toolData.length === 0) {
    // Empty tool list is a sidecar misconfiguration — usually the
    // parent stdio process couldn't dump the metadata cache. Tell
    // the user where to look instead of leaving them on "Loading".
    updateStatus(
      'No tools found. The sidecar reads ~/.ha-mcp/tool_metadata.json — ' +
      'if missing/empty, restart your MCP client. See ~/.ha-mcp/sidecar.log for details.'
    );
    return;
  }
  try {
    render();
  } catch (e) {
    updateStatus('Render failed: ' + e.message + ' (open browser devtools for the stack)');
    throw e;
  }
  updateStatus('Loaded');

  // Show restart button if running as add-on; show Stop Sidecar
  // button only when this page is served by the stdio sidecar
  // (HTTP modes serve the same HTML but is_sidecar=false there, so
  // clicking Stop wouldn't make sense — it would kill the MCP server).
  // Also tailor the restart-notice copy to the install mode so the
  // user is told exactly what action they need to take ("close and
  // reopen Claude Desktop" vs "click Restart Add-on" vs "restart
  // your Docker container") instead of a generic "restart the add-on"
  // that only matches one of three real deployment surfaces.
  try {
    const infoResp = await fetch('./api/settings/info');
    const info = await infoResp.json();
    const noticeEl = document.getElementById('restartNoticeText');
    if (info.is_addon) {
      document.getElementById('restartBtn').style.display = '';
      if (noticeEl) {
        noticeEl.textContent =
          '⚠ Changes saved. Click "Restart Add-on" for them to take ' +
          'effect — disabled tools will be fully removed from the MCP ' +
          'tool list on next startup.';
      }
    } else if (info.is_sidecar) {
      if (noticeEl) {
        noticeEl.textContent =
          '⚠ Changes saved. Fully quit and reopen your MCP client ' +
          '(Claude Desktop: right-click the tray icon → Quit, then ' +
          'relaunch; Claude Code: close the terminal session) for them ' +
          'to take effect. Disabled tools will be fully removed from the ' +
          'MCP tool list on next startup.';
      }
      document.getElementById('sidecarStopRow').style.display = '';
    } else if (noticeEl) {
      // HTTP / Docker / standalone — no button we can wire to a restart,
      // so describe the action in process terms.
      noticeEl.textContent =
        '⚠ Changes saved. Restart your ha-mcp process (Docker ' +
        'container, systemd service, or however you launch it) for them ' +
        'to take effect. Disabled tools will be fully removed from the ' +
        'MCP tool list on next startup.';
    }
  } catch (_e) {}
}

async function stopSidecar() {
  const btn = document.getElementById('stopSidecarBtn');
  // Two-part confirm wording: lead with the *permanence* (this is not a
  // routine "stop now, autostart later" — the server will refuse to
  // restart on every future ha-mcp launch until the user manually
  // intervenes), then spell out the exact re-enable steps. The button
  // is right-aligned near the top of a list of toggle controls, so
  // accidental clicks are easy; the dialog needs to read like a
  // commitment, not a soft prompt.
  if (!confirm(
    '⚠ PERMANENTLY disable the settings server?\\n\\n' +
    'This stops the running server AND writes a disable marker so it ' +
    'will NOT respawn on future ha-mcp launches — every restart of ' +
    'Claude Desktop / Docker / your MCP host will continue to skip it ' +
    'until you manually re-enable.\\n\\n' +
    'To restore access later you must:\\n' +
    '  1. Delete  ~/.ha-mcp/settings_ui_disabled  (the marker file), AND\\n' +
    '  2. Unset  HA_MCP_DISABLE_SETTINGS_UI  if that env var was set.\\n\\n' +
    'You will lose the in-browser tool-configuration UI until both ' +
    'conditions are met. Continue?'
  )) return;
  btn.disabled = true;
  btn.textContent = 'Stopping...';
  try {
    const resp = await fetch('./api/settings/shutdown', {method: 'POST'});
    if (resp.ok) {
      btn.textContent = 'Stopped — this page will go offline';
    } else {
      let msg = 'Stop failed';
      try {
        const err = await resp.json();
        if (err.error && err.error.message) msg = 'Failed: ' + err.error.message;
      } catch (_e) {}
      btn.textContent = msg;
      btn.disabled = false;
      alert(msg);
    }
  } catch (_e) {
    // Connection drop is expected — the sidecar process is exiting.
    btn.textContent = 'Stopped (connection dropped)';
  }
}

// Restart-readiness probe tunables. The grace period gives supervisor
// time to actually kill the addon (so a too-eager first probe doesn't
// hit the OLD instance and reload before the new one is up). The poll
// interval is short enough to feel responsive on a fast restart, long
// enough to not hammer ingress. The cap is the user-visible upper
// bound; HAOS addon restarts are typically 15-25s but cold-start +
// image pull can stretch further, so 60s gives genuine breathing room
// before we tell the user the auto-reload failed.
const RESTART_PROBE_INITIAL_GRACE_MS = 3000;
const RESTART_PROBE_INTERVAL_MS = 2000;
const RESTART_PROBE_MAX_TOTAL_MS = 60000;

// Cross-tab restart broadcast channel. When any tab saves a setting
// that needs a restart, it posts ``restart-required`` so the other
// tabs surface the same banner. When any tab fires the supervisor
// restart, it posts ``restart-initiated`` so the other tabs run the
// same poll-then-reload cycle — that way ALL tabs come back to the
// fresh addon instead of leaving stale ones spinning.
const restartChannel =
  typeof BroadcastChannel === 'function'
    ? new BroadcastChannel('ha-mcp-settings')
    : null;

// Module-level concurrency guard. The button's ``disabled`` attribute
// blocks normal clicks, but a second invocation via DevTools / a
// keyboard accessibility tool / a cross-tab broadcast would otherwise
// queue a second supervisor restart + a second auto-reload. Cleared
// only on a 4xx genuine config error (so the user can reload and try
// again); otherwise stays true through the restart cycle until the
// page reloads.
let restartInProgress = false;

async function _fetchSettingsInfo() {
  // Read ``/api/settings/info`` once; return the parsed JSON or null
  // on any failure. ``cache: 'no-store'`` so the browser can't serve
  // a stale 200 from before the restart.
  try {
    const resp = await fetch('./api/settings/info', {cache: 'no-store'});
    if (!resp.ok) return null;
    return await resp.json();
  } catch (_e) {
    return null;
  }
}

async function _probeAddonRestarted(previousInstanceId) {
  // Resolve true when ``/api/settings/info`` returns a different
  // ``instance_id`` than the one captured before the restart —
  // proves a NEW process is serving, not the same OLD one (which
  // would happen if supervisor silently failed to restart and the
  // probe just saw the still-running upstream answer 200). When
  // ``previousInstanceId`` is null (couldn't capture pre-restart,
  // or server is on an older build that doesn't expose the field)
  // fall back to "any 200 means it's back" — same behavior as
  // before this fix landed, so we degrade gracefully.
  const deadline = Date.now() + RESTART_PROBE_MAX_TOTAL_MS;
  while (Date.now() < deadline) {
    const info = await _fetchSettingsInfo();
    if (info) {
      if (previousInstanceId) {
        if (info.instance_id && info.instance_id !== previousInstanceId) {
          return true;
        }
        // Same instance_id (or field missing on the response) — keep
        // polling; do NOT reload yet because the restart hasn't
        // actually happened yet.
      } else {
        // No baseline to compare against — best we can do is the
        // old "200 = up" check.
        return true;
      }
    }
    await new Promise(r => setTimeout(r, RESTART_PROBE_INTERVAL_MS));
  }
  return false;
}

async function _runRestartReloadCycle(previousInstanceId) {
  const btn = document.getElementById('restartBtn');
  // Initial grace lets supervisor actually kill the addon before we
  // start probing — otherwise the first probe may hit the OLD
  // instance and we reload before the new one is up.
  btn.textContent = 'Restarting…';
  await new Promise(r => setTimeout(r, RESTART_PROBE_INITIAL_GRACE_MS));
  btn.textContent = 'Waiting for add-on to come back online…';
  const restarted = await _probeAddonRestarted(previousInstanceId);
  if (restarted) {
    window.location.reload();
  } else {
    // Probe gave up after RESTART_PROBE_MAX_TOTAL_MS. Restart either
    // never actually fired (silent supervisor failure → instance_id
    // never flipped) OR supervisor is genuinely slower than the cap.
    // Surface a clear next-step instead of silently doing nothing.
    btn.textContent = 'Add-on did not come back online — reload manually';
    btn.disabled = false;
    restartInProgress = false;
  }
}

async function restartAddon() {
  if (restartInProgress) return;
  const btn = document.getElementById('restartBtn');
  if (!confirm('Restart the add-on now? The page will reload automatically once the add-on is back online.')) return;
  restartInProgress = true;
  btn.disabled = true;
  btn.textContent = 'Restarting…';
  // Capture the current process's ``instance_id`` BEFORE firing the
  // restart so the poll cycle has a baseline to compare against.
  // null is fine — the probe degrades to the old "any 200 means up"
  // mode rather than refusing to reload.
  const info = await _fetchSettingsInfo();
  const previousInstanceId = info?.instance_id ?? null;
  try {
    const resp = await fetch('./api/settings/restart', {method: 'POST'});
    if (!resp.ok && resp.status < 500) {
      // 4xx is a genuine config error (e.g. SUPERVISOR_TOKEN unset).
      // The restart was NOT initiated — surface the error and let the
      // user fix the underlying cause. Keep button enabled so they
      // can retry once the issue is resolved. Don't broadcast (other
      // tabs would only see a misleading "restart in progress").
      let msg = 'Restart failed';
      try {
        const err = await resp.json();
        if (err?.error?.message) msg = 'Failed: ' + err.error.message;
      } catch (_e) { /* leave default msg */ }
      btn.textContent = msg;
      btn.disabled = false;
      restartInProgress = false;
      alert(msg);
      return;
    }
    // 200 OK → background task scheduled. 5xx → ingress upstream
    // drop, restart IS in flight. Both fall through to the reload
    // cycle.
  } catch (_e) {
    // Network error mid-request — supervisor killed our upstream.
    // Restart in flight; fall through. Log for debug, suppress the
    // unused-binding lint.
    console.warn('restartAddon fetch dropped (expected during self-restart):', _e);
  }
  // Other tabs need to run the same cycle so they reload to the fresh
  // addon, not stay on a stale view. Broadcast the baseline so each
  // tab compares against the same pre-restart ``instance_id``.
  if (restartChannel) {
    restartChannel.postMessage({
      type: 'restart-initiated',
      previousInstanceId,
    });
  }
  await _runRestartReloadCycle(previousInstanceId);
}

// Listener: when ANY tab broadcasts a save that needs a restart, all
// open tabs surface the banner. When ANY tab fires the restart, all
// open tabs run their own poll-then-reload cycle so none of them are
// left holding a stale connection to a now-dead addon.
if (restartChannel) {
  restartChannel.addEventListener('message', (e) => {
    const data = e.data || {};
    if (data.type === 'restart-required') {
      document.getElementById('restartNotice').classList.add('show');
    } else if (data.type === 'restart-initiated' && !restartInProgress) {
      restartInProgress = true;
      const btn = document.getElementById('restartBtn');
      if (btn) btn.disabled = true;
      // Use the originating tab's baseline ``instance_id`` so every
      // tab waits for the SAME ``instance_id`` flip before reloading.
      // Falls back to null → "any 200 = ready" mode if the originator
      // couldn't capture one.
      _runRestartReloadCycle(data.previousInstanceId ?? null);
    }
  });
}

const DEFAULT_PINNED = """
    + json.dumps(list(DEFAULT_PINNED_TOOLS))
    + """;
const MANDATORY = """
    + json.dumps(list(MANDATORY_TOOLS))
    + """;

function getState(name) {
  if (toolStates[name]) return toolStates[name];
  return DEFAULT_PINNED.includes(name) ? 'pinned' : 'enabled';
}

// Escape HTML special characters before interpolating into innerHTML.
// All interpolated values come from the server (tool docstrings, names,
// FEATURE_GATED_TOOLS metadata) so this is defense-in-depth — but a
// docstring containing literal '<' or '&' would otherwise break the
// page silently.
function escapeHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function render() {
  const groups = {};
  toolData.forEach(t => {
    const tag = t.primary_tag || (t.tags && t.tags[0]) || 'Other';
    if (!groups[tag]) groups[tag] = [];
    groups[tag].push(t);
  });

  const container = document.getElementById('groups');
  container.innerHTML = '';

  let total = 0, enabledCount = 0, pinnedCount = 0, disabledCount = 0;

  Object.keys(groups).sort().forEach(tag => {
    const tools = groups[tag];
    const group = document.createElement('div');
    group.className = 'group';

    // Per-group toggle state: enabled if ANY non-mandatory/non-gated/non-env-pinned tool is enabled
    const toggleable = tools.filter(t =>
      !MANDATORY.includes(t.name) && !t.disabled_by && !toolEnvPinned[t.name]);
    const anyEnabled = toggleable.some(t => getState(t.name) !== 'disabled');
    const groupEnabled = tools.filter(t => {
      if (toolEnvPinned[t.name]) return toolEnvPinned[t.name] !== 'disabled';
      const s = getState(t.name);
      return MANDATORY.includes(t.name) || (!t.disabled_by && s !== 'disabled');
    }).length;

    const header = document.createElement('div');
    header.className = 'group-header';
    header.innerHTML = `<div class="group-header-left">` +
      `<span class="group-chevron">&#9654;</span>` +
      `<span class="group-name">${escapeHtml(tag)}</span>` +
      `<span class="group-count">${groupEnabled}/${tools.length} enabled</span>` +
      `</div>` +
      `<label class="switch group-master" title="Enable/disable all tools in this group">` +
        `<input type="checkbox" ${anyEnabled ? 'checked' : ''} ${toggleable.length === 0 ? 'disabled' : ''}>` +
        `<span class="slider"></span>` +
      `</label>`;

    const chevron = header.querySelector('.group-chevron');
    const masterInput = header.querySelector('.group-master input');

    header.addEventListener('click', (e) => {
      // Ignore clicks on the master toggle itself
      if (e.target.closest('.group-master')) return;
      if (openGroups.has(tag)) openGroups.delete(tag);
      else openGroups.add(tag);
      const toolsDiv = group.querySelector('.group-tools');
      toolsDiv.classList.toggle('open');
      chevron.classList.toggle('open');
    });

    if (masterInput) {
      masterInput.addEventListener('click', (e) => e.stopPropagation());
      masterInput.addEventListener('change', (e) => {
        const target = e.target.checked ? 'enabled' : 'disabled';
        toggleable.forEach(t => {
          if (target === 'enabled') {
            // Restore to pinned if it was pinned by default, else enabled
            toolStates[t.name] = DEFAULT_PINNED.includes(t.name) ? 'pinned' : 'enabled';
          } else {
            toolStates[t.name] = 'disabled';
          }
        });
        scheduleSave();
        render();
      });
    }

    const toolsDiv = document.createElement('div');
    toolsDiv.className = 'group-tools';
    if (openGroups.has(tag)) {
      toolsDiv.classList.add('open');
      chevron.classList.add('open');
    }

    tools.forEach(t => {
      const state = getState(t.name);
      const isMandatory = MANDATORY.includes(t.name);
      const disabledBy = t.disabled_by || null;
      const isFeatureGated = disabledBy !== null;
      // env_pinned: "disabled" | "pinned" | undefined — operator-level lock
      // via DISABLED_TOOLS / PINNED_TOOLS env vars. When set, all inputs are
      // disabled and a banner names the env var. Takes precedence over
      // isMandatory / isFeatureGated for the lock calculation.
      const envPinKind = toolEnvPinned[t.name]; // "disabled" | "pinned" | undefined
      const isEnvPinned = !!envPinKind;
      const envPinVar = envPinKind === 'disabled' ? 'DISABLED_TOOLS' :
                        envPinKind === 'pinned'   ? 'PINNED_TOOLS'   : '';
      const ann = t.annotations || {};
      const isReadOnly = ann.readOnlyHint === true;
      const isDestructive = ann.destructiveHint === true;

      total++;
      if (isEnvPinned) {
        if (envPinKind === 'disabled') disabledCount++;
        else { enabledCount++; pinnedCount++; }
      } else if (isFeatureGated) disabledCount++;
      else if (state === 'disabled') disabledCount++;
      else if (state === 'pinned') { enabledCount++; pinnedCount++; }
      else enabledCount++;

      const isEnabled = isEnvPinned
        ? (envPinKind !== 'disabled')
        : (isFeatureGated ? false : (isMandatory || state !== 'disabled'));
      const isPinned = isEnvPinned
        ? (envPinKind === 'pinned')
        : (isFeatureGated ? false : (isMandatory || state === 'pinned' || DEFAULT_PINNED.includes(t.name)));
      const lockEnabled = isEnvPinned || isMandatory || isFeatureGated;
      const lockPinned = isEnvPinned || isMandatory || isFeatureGated || !isEnabled;

      const div = document.createElement('div');
      div.className = isEnvPinned ? 'tool env-pinned' : 'tool';
      div.dataset.name = t.name.toLowerCase();
      div.dataset.title = (t.title || '').toLowerCase();

      let badges = '';
      if (isMandatory) badges += '<span class="badge mandatory">mandatory</span>';
      if (isReadOnly) badges += '<span class="badge readonly">read-only</span>';
      if (isDestructive) badges += '<span class="badge destructive">destructive</span>';

      const title = t.title || t.name;
      const desc = (t.description || '').split('\\n')[0].slice(0, 120);
      const gatedNote = disabledBy
        ? `<div class="disabled-by-note">Beta — set <code>${escapeHtml(disabledBy)}</code> in the dev add-on config or the matching env var (see docs/beta.md).</div>`
        : '';
      const envPinnedNote = isEnvPinned
        ? `<div class="feature-locked-note">env-pinned via <code>${envPinVar}</code> — unset the env var to edit here.</div>`
        : '';

      div.innerHTML = `<div class="tool-info">` +
        `<div class="tool-name">${escapeHtml(title)}${badges}</div>` +
        `<div class="tool-meta">${escapeHtml(t.name)}</div>` +
        (desc ? `<div class="tool-desc">${escapeHtml(desc)}</div>` : '') +
        gatedNote +
        envPinnedNote +
        `</div>` +
        `<div class="tool-toggles">` +
          `<div class="toggle-group">` +
            `<label class="switch"><input type="checkbox" data-tool="${escapeHtml(t.name)}" data-field="enabled" ` +
              `${isEnabled ? 'checked' : ''} ${lockEnabled ? 'disabled' : ''}>` +
              `<span class="slider"></span></label>` +
            `<span>enabled</span>` +
          `</div>` +
          `<div class="toggle-group ${!isEnabled ? 'disabled-toggle' : ''}">` +
            `<label class="switch"><input type="checkbox" data-tool="${escapeHtml(t.name)}" data-field="pinned" ` +
              `${isPinned ? 'checked' : ''} ${lockPinned ? 'disabled' : ''}>` +
              `<span class="slider"></span></label>` +
            `<span>pinned</span>` +
          `</div>` +
          `<div class="toggle-group ${(policyState.enabled && isEnabled) ? '' : 'disabled-toggle'}" ` +
               `title="${policyState.enabled ? '' : 'Enable Tool Security Policies in addon config first.'}">` +
            `<label class="switch"><input type="checkbox" data-tool="${escapeHtml(t.name)}" data-field="gated" ` +
              `${policyState.gatedTools.has(t.name) ? 'checked' : ''} ` +
              `${(policyState.enabled && isEnabled) ? '' : 'disabled'}>` +
              `<span class="slider"></span></label>` +
            `<span>security gated</span>` +
          `</div>` +
        `</div>`;

      const inputs = div.querySelectorAll('input[type="checkbox"]');
      inputs.forEach(input => {
        if (input.disabled) return;
        input.addEventListener('change', async (e) => {
          const field = e.target.dataset.field;
          if (field === 'gated') {
            // Optimistic UI: flip local state, sync to server, rollback on failure.
            // Gated lives in policy.rules (not tool_config), so we skip scheduleSave().
            const wasGated = policyState.gatedTools.has(t.name);
            const nowGated = e.target.checked;
            if (nowGated) policyState.gatedTools.add(t.name);
            else policyState.gatedTools.delete(t.name);
            try {
              await syncPolicyRule(t.name, nowGated);
            } catch (err) {
              if (wasGated) policyState.gatedTools.add(t.name);
              else policyState.gatedTools.delete(t.name);
              e.target.checked = wasGated;
              alert('Failed to update tool security policy: ' + err.message);
            }
            render();
            return;
          }
          const currentState = getState(t.name);
          let newState = currentState;
          if (field === 'enabled') {
            if (!e.target.checked) newState = 'disabled';
            else newState = (currentState === 'pinned') ? 'pinned' : 'enabled';
          } else if (field === 'pinned') {
            newState = e.target.checked ? 'pinned' : 'enabled';
          }
          toolStates[t.name] = newState;
          scheduleSave();
          render();
        });
      });
      toolsDiv.appendChild(div);
    });

    group.appendChild(header);
    group.appendChild(toolsDiv);
    container.appendChild(group);
  });

  document.getElementById('summary').innerHTML =
    `<span>${total} total</span>` +
    `<span style="color:var(--success)">${enabledCount} enabled</span>` +
    `<span style="color:var(--accent)">${pinnedCount} pinned</span>` +
    `<span style="color:var(--danger)">${disabledCount} disabled</span>`;

  // ``render()`` rebuilds the entire ``.tool`` DOM, so any
  // ``hidden`` class previously applied by ``applyToolSearch`` is
  // wiped. The search ``<input>`` is a separate element and keeps
  // its value across the rebuild — re-apply the filter so the
  // visible list matches what the user has typed. Otherwise
  // toggling a setting on a filtered tool snaps the full list back
  // even though the search box still shows the query.
  applyToolSearch();
}

function scheduleSave() {
  clearTimeout(saveTimer);
  updateStatus('Unsaved changes...');
  saveTimer = setTimeout(saveConfig, 800);
}

async function saveConfig() {
  updateStatus('Saving...');
  const resp = await fetch('./api/settings/tools', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({states: toolStates}),
  });
  if (resp.ok) {
    updateStatus('Saved — restart required', true);
    document.getElementById('restartNotice').classList.add('show');
    // Cross-tab sync — other open settings tabs surface the same
    // banner so the user can click Restart from whichever tab they
    // are on.
    if (restartChannel) restartChannel.postMessage({type: 'restart-required'});
  } else {
    updateStatus('Save failed!');
  }
}

function updateStatus(text, saved) {
  const el = document.getElementById('status');
  el.textContent = text;
  el.className = saved ? 'status saved' : 'status';
}

function applyToolSearch() {
  // Read the current search query directly from the DOM rather than
  // taking it as a parameter — ``render()`` calls this after rebuilding
  // the tool DOM and needs to use whatever the user currently has
  // typed without coordinating with the input event.
  const q = (document.getElementById('search').value || '').toLowerCase();
  document.querySelectorAll('.tool').forEach(el => {
    const match = !q || el.dataset.name.includes(q) || el.dataset.title.includes(q);
    el.classList.toggle('hidden', !match);
  });
  document.querySelectorAll('.group').forEach(g => {
    const tools = g.querySelector('.group-tools');
    const visible = tools.querySelectorAll('.tool:not(.hidden)').length;
    g.style.display = visible ? '' : 'none';
    if (q && visible) {
      tools.classList.add('open');
      g.querySelector('.group-chevron').classList.add('open');
    }
  });
}

document.getElementById('search').addEventListener('input', applyToolSearch);

document.getElementById('restartBtn').addEventListener('click', restartAddon);

// ===== Backups tab =====
let backupEntries = [];
let backupConfigFields = [];

const BACKUP_FIELD_LABELS = {
  enable_auto_backup: {
    label: 'Auto-backup edits',
    help: 'Capture a snapshot before every wrapped write/destructive tool call.',
  },
  auto_backup_throttle_minutes: {
    label: 'Throttle (minutes)',
    help: 'Per-entity throttle. 0 = backup every write; N>0 = at most one per N minutes per entity. Range 0–1440.',
  },
  auto_backup_retain_per_entity: {
    label: 'Retain per entity',
    help: 'Maximum snapshots kept per entity (1–10000). Older ones rotate out.',
  },
  auto_backup_dir: {
    label: 'Backup directory override',
    help: 'Empty = default (/data/ha_mcp_backups in the add-on, $XDG_DATA_HOME/ha_mcp/backups otherwise). Override with an absolute path.',
  },
  auto_backup_calendar_lookahead_days: {
    label: 'Calendar lookahead (days)',
    help: 'How far ahead to query for calendar events when capturing pre-edit snapshots. Range 1–365.',
  },
};

const BACKUP_ORIGIN_LABELS = {
  addon: 'Synced to Supervisor — restart required after save.',
  env: null,  // banner generated dynamically with the env var name
  file: 'Persisted locally; takes effect immediately.',
  default: 'Using default; first save creates a local override file.',
};

async function loadBackupConfig() {
  const formEl = document.getElementById('backupConfigForm');
  const actionsEl = document.getElementById('backupConfigActions');
  try {
    const resp = await fetch('./api/settings/backup-config');
    if (!resp.ok) {
      formEl.innerHTML = '<div class="backup-empty">Could not load backup settings.</div>';
      actionsEl.style.display = 'none';
      return;
    }
    const data = await resp.json();
    backupConfigFields = data.fields || [];
    if (typeof data.is_addon === 'boolean') {
      IS_ADDON_MODE = data.is_addon;
    }
  } catch (_e) {
    formEl.innerHTML = '<div class="backup-empty">Backup settings unavailable.</div>';
    actionsEl.style.display = 'none';
    return;
  }
  renderBackupConfig();
  actionsEl.style.display = backupConfigFields.some(f => f.editable) ? '' : 'none';
}

function renderBackupConfig() {
  const formEl = document.getElementById('backupConfigForm');
  formEl.innerHTML = '';
  backupConfigFields.forEach(f => {
    const meta = BACKUP_FIELD_LABELS[f.field] || { label: f.field, help: '' };
    const row = document.createElement('div');
    row.className = 'backup-field';
    let controlHtml;
    if (typeof f.value === 'boolean') {
      controlHtml = `<input type="checkbox" data-field="${escapeHtml(f.field)}" ${f.value ? 'checked' : ''} ${f.editable ? '' : 'disabled'}>`;
    } else if (typeof f.value === 'string') {
      // Path / freeform string fields (auto_backup_dir).
      controlHtml = `<input type="text" data-field="${escapeHtml(f.field)}" value="${escapeHtml(String(f.value ?? ''))}" ${f.editable ? '' : 'disabled'}>`;
    } else {
      let min = 1;
      let max = 10000;
      if (f.field === 'auto_backup_throttle_minutes') { min = 0; max = 1440; }
      else if (f.field === 'auto_backup_calendar_lookahead_days') { min = 1; max = 365; }
      controlHtml = `<input type="number" data-field="${escapeHtml(f.field)}" value="${Number(f.value)}" min="${min}" max="${max}" ${f.editable ? '' : 'disabled'}>`;
    }
    let originMsg;
    if (f.origin === 'env') {
      originMsg = envLockedNoteHtml(f.env_var, f.field);
    } else {
      originMsg = BACKUP_ORIGIN_LABELS[f.origin] || '';
    }
    const lockedBadge = f.editable ? '' : `<span class="backup-field-locked">env-locked</span>`;
    row.innerHTML =
      `<span class="backup-field-label">${escapeHtml(meta.label)}</span>` +
      `<span class="backup-field-control">${controlHtml}</span>` +
      lockedBadge +
      `<span class="backup-field-help">${escapeHtml(meta.help)}${originMsg ? ' — ' + originMsg : ''}</span>`;
    formEl.appendChild(row);
  });
}

async function saveBackupConfig() {
  const btn = document.getElementById('backupConfigSave');
  const statusEl = document.getElementById('backupConfigStatus');
  const payload = {};
  backupConfigFields.forEach(f => {
    if (!f.editable) return;
    const input = document.querySelector(`#backupConfigForm input[data-field="${f.field}"]`);
    if (!input) return;
    if (input.type === 'checkbox') {
      payload[f.field] = input.checked;
    } else if (input.type === 'text') {
      payload[f.field] = input.value;
    } else {
      const n = parseInt(input.value, 10);
      if (!isNaN(n)) payload[f.field] = n;
    }
  });
  if (Object.keys(payload).length === 0) {
    statusEl.textContent = 'Nothing editable.';
    return;
  }
  btn.disabled = true;
  statusEl.textContent = 'Saving…';
  try {
    const resp = await fetch('./api/settings/backup-config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok) {
      btn.disabled = false;
      let msg = 'Save failed';
      if (data && data.error) {
        if (typeof data.error === 'string') msg = data.error;
        else if (data.error.message) msg = data.error.message;
      }
      statusEl.textContent = msg;
      return;
    }
    btn.disabled = false;
    if (data.restart_required) {
      // Unified restart flow — save persists but does NOT auto-restart.
      // Surface the cross-tab restart-required banner; user picks the
      // moment via the global Restart Add-on button.
      //
      // Don't reload the form here. In addon mode the GET reads
      // env-derived ``get_global_settings()`` values which are still
      // stale (Supervisor has the new options but ``start.py``
      // doesn't re-derive env vars until the next addon boot). Reloading
      // would snap the form back to old values, look like the save
      // reverted, and clobber any further edits the user wanted to
      // bundle before clicking Restart.
      statusEl.textContent = 'Saved — restart required';
      document.getElementById('restartNotice').classList.add('show');
      if (restartChannel) restartChannel.postMessage({type: 'restart-required'});
    } else {
      statusEl.textContent = 'Saved.';
      // Refresh display so origins update (default → file, etc.).
      loadBackupConfig();
      loadBackups();
    }
  } catch (err) {
    btn.disabled = false;
    statusEl.textContent = 'Network error: ' + String(err);
  }
}

async function loadBackups() {
  const params = new URLSearchParams();
  const d = document.getElementById('backupDomain').value.trim();
  const e = document.getElementById('backupEntity').value.trim();
  if (d) params.set('domain', d);
  if (e) params.set('entity_id', e);
  const stateEl = document.getElementById('backupState');
  const listEl = document.getElementById('backupList');
  try {
    const resp = await fetch('./api/settings/backups?' + params.toString());
    const data = await resp.json();
    if (!resp.ok || !data.success) {
      stateEl.innerHTML = '<span class="diff-rem">Error loading backups</span>';
      listEl.innerHTML = '';
      return;
    }
    backupEntries = data.backups || [];
    stateEl.innerHTML =
      `<span>Status: <strong>${data.enabled ? 'enabled' : 'disabled'}</strong></span>` +
      `<span>Throttle: <strong>${data.throttle_minutes} min</strong></span>` +
      `<span>Retain per entity: <strong>${data.retain_per_entity}</strong></span>` +
      `<span>Directory: <strong>${escapeHtml(data.backup_dir)}</strong></span>` +
      `<span>Total: <strong>${data.count}</strong></span>`;
    renderBackups();
  } catch (err) {
    stateEl.innerHTML = '<span class="diff-rem">Network error: ' + escapeHtml(String(err)) + '</span>';
    listEl.innerHTML = '';
  }
}

function renderBackups() {
  const listEl = document.getElementById('backupList');
  if (!backupEntries.length) {
    listEl.innerHTML = '<div class="backup-empty">No backups yet. Enable auto-backup in the add-on config and edit an entity to create one.</div>';
    return;
  }
  listEl.innerHTML = '';
  backupEntries.forEach(b => {
    const row = document.createElement('div');
    row.className = 'backup-row';
    const ts = b.timestamp || '';
    const tsFmt = ts.length === 15
      ? ts.slice(0,4)+'-'+ts.slice(4,6)+'-'+ts.slice(6,8)+' '+ts.slice(9,11)+':'+ts.slice(11,13)+':'+ts.slice(13,15)
      : ts;
    row.innerHTML =
      `<div class="backup-row-info">` +
        `<div class="backup-row-name">${escapeHtml(b.name)}</div>` +
        `<div class="backup-row-meta">` +
          `<strong>${escapeHtml(b.domain)}</strong> · ` +
          `${escapeHtml(b.entity_id)} · ${tsFmt} · ${b.size} bytes` +
        `</div>` +
      `</div>` +
      `<div class="backup-row-actions">` +
        `<button data-act="view">View</button>` +
        `<button data-act="diff" class="secondary">Diff</button>` +
        `<button data-act="restore">Restore</button>` +
        `<button data-act="delete" class="danger">Delete</button>` +
      `</div>`;
    row.querySelectorAll('button[data-act]').forEach(btn => {
      btn.addEventListener('click', () => backupAction(btn.dataset.act, b.name));
    });
    listEl.appendChild(row);
  });
}

async function backupAction(act, name) {
  if (act === 'view') {
    const resp = await fetch('./api/settings/backups/' + encodeURIComponent(name));
    const data = await resp.json();
    if (!resp.ok) { alert(JSON.stringify(data)); return; }
    showModal('View: ' + name, '<pre>' + escapeHtml(yamlStringify(data.data)) + '</pre>');
  } else if (act === 'diff') {
    const resp = await fetch('./api/settings/backups/' + encodeURIComponent(name) + '/diff');
    const data = await resp.json();
    if (!resp.ok) { alert(JSON.stringify(data)); return; }
    const html = (data.diff || '(identical)').split('\\n').map(line => {
      let cls = '';
      if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('@@')) cls = 'diff-hdr';
      else if (line.startsWith('+')) cls = 'diff-add';
      else if (line.startsWith('-')) cls = 'diff-rem';
      return `<span class="${cls}">${escapeHtml(line)}</span>`;
    }).join('\\n');
    showModal('Diff: ' + name, '<pre>' + html + '</pre>');
  } else if (act === 'restore') {
    if (!confirm('Restore ' + name + '?\\n\\nThis will overwrite the current entity state. A safety backup of the current state is taken first.')) return;
    const resp = await fetch('./api/settings/backups/' + encodeURIComponent(name) + '/restore', {method: 'POST'});
    const data = await resp.json();
    if (!resp.ok) { alert('Restore failed: ' + JSON.stringify(data)); return; }
    alert('Restored. Safety backup: ' + (data.data && data.data.safety_backup ? data.data.safety_backup : '(none)'));
    loadBackups();
  } else if (act === 'delete') {
    if (!confirm('Delete ' + name + '? This cannot be undone.')) return;
    const resp = await fetch('./api/settings/backups/' + encodeURIComponent(name), {method: 'DELETE'});
    if (!resp.ok) { const d = await resp.json(); alert('Delete failed: ' + JSON.stringify(d)); return; }
    loadBackups();
  }
}

async function bulkDeleteBackups() {
  const d = document.getElementById('backupDomain').value.trim();
  const e = document.getElementById('backupEntity').value.trim();
  const days = prompt('Delete backups older than N days (leave blank to use current filters only):', '');
  const params = new URLSearchParams();
  if (d) params.set('domain', d);
  if (e) params.set('entity_id', e);
  if (days) params.set('older_than_days', days);
  if (!params.toString()) { alert('Set at least one filter (Domain, Entity, or age in days).'); return; }
  if (!confirm('Delete all backups matching: ' + params.toString() + '?')) return;
  const resp = await fetch('./api/settings/backups?' + params.toString(), {method: 'DELETE'});
  const data = await resp.json();
  if (!resp.ok) { alert('Bulk delete failed: ' + JSON.stringify(data)); return; }
  alert('Deleted ' + (data.count || 0) + ' backup(s)');
  loadBackups();
}

function showModal(title, html) {
  document.getElementById('modalTitle').textContent = title;
  document.getElementById('modalBody').innerHTML = html;
  document.getElementById('modalBackdrop').classList.add('show');
}
function closeModal() { document.getElementById('modalBackdrop').classList.remove('show'); }

// Pretty-print the snapshot envelope for the view modal. The server
// returns the parsed YAML as JSON; indented JSON is the simplest
// readable form for the modal without pulling in a JS YAML library.
function yamlStringify(obj) { return JSON.stringify(obj, null, 2); }

document.getElementById('backupRefresh').addEventListener('click', loadBackups);
document.getElementById('backupBulkDelete').addEventListener('click', bulkDeleteBackups);
document.getElementById('backupConfigSave').addEventListener('click', saveBackupConfig);
document.getElementById('modalClose').addEventListener('click', closeModal);
document.getElementById('modalBackdrop').addEventListener('click', (e) => {
  if (e.target.id === 'modalBackdrop') closeModal();
});

document.getElementById('stopSidecarBtn').addEventListener('click', stopSidecar);

// Feature-flag metadata (display labels + help text). Keyed by the
// Settings field name. The strings are intentionally copied verbatim
// from ``homeassistant-addon-dev/translations/en.yaml`` so the web
// UI and the add-on Configuration tab read identically — a user who
// flips between the two surfaces never wonders if the option name
// or warning text shifted meaning. Keep them in sync when one side
// changes; the addon-dev translations file is the source of truth.
const FEATURE_META = {
  enable_tool_search: {
    label: "Enable tool search",
    help: "Replace the full tool catalog with search-based discovery. Reduces idle context from ~46K to ~5K tokens. ⚠️ Do NOT enable this if you use Claude in Sonnet or Opus modes — those models have their own built-in tool search / deferred tools, which conflicts with ours. To use ha-mcp's tool search with Claude, disable Claude's built-in tool search first; otherwise leave this off. Use this only with LLMs that lack native deferred tools (e.g. Claude Haiku, local OpenAI-compatible models) or with smaller context windows. Tools are found via ha_search_tools and executed via categorized proxies (read/write/delete). Requires restart to take effect.",
  },
  tool_search_max_results: {
    label: "Tool search max results",
    help: "Maximum number of tools returned by ha_search_tools when tool search is enabled. Lower values (2-3) save context tokens but may miss relevant tools. Range: 2-10. Requires restart.",
  },
  enable_tool_security_policies: {
    label: "Enable Tool Security Policies",
    help: "Opt-in middleware that gates high-stakes MCP tool calls behind user approval. When enabled, tools that match a rule in the Tool Security Policies tab require you to click Approve in the web UI before they run. Off by default. Per-tool rules with optional argument conditions are configured in the Tool Security Policies tab. Requires restart to take effect.",
  },
  // Master beta toggle — gates the 5 sub-flags below at runtime
  // (see config.py:_apply_feature_flag_overrides master gate). UI
  // dims sub-rows when this is off and re-renders live on flip.
  enable_beta_features: {
    label: "Enable beta features",
    help: "⚠ DANGER — these tools can PERMANENTLY DAMAGE your Home Assistant installation. They write to your YAML config, your filesystem, install custom components, run arbitrary sandboxed Python, and edit tool docstrings the AI sees. There is no warranty and no support guarantee — you enable them at your OWN RISK. Take a Home Assistant backup before turning this on, and never enable in production without one. Master toggle for the 5 experimental tools below; sub-toggles are dimmed and ignored at runtime while this is off (even a sub-flag set via env var is forced off until the master is on). Requires restart to take effect.",
  },
  enable_yaml_config_editing: {
    label: "Enable YAML config editing (beta)",
    help: "Beta feature — disabled by default. Allows AI assistants to add, replace, or remove top-level keys in configuration.yaml and packages/*.yaml. Only whitelisted keys are allowed (e.g., template, sensor, command_line, mqtt, knx); core keys like homeassistant, http, and recorder are blocked. Each edit validates YAML syntax, runs a config check, and creates an automatic backup. Changes to most keys require a full HA restart to take effect. See docs/beta.md for known limitations. Dedicated tools (automations, scripts, scenes, helpers, template sensors) should be preferred when available.",
  },
  enable_filesystem_tools: {
    label: "Enable filesystem tools (beta)",
    help: "Sets HAMCP_ENABLE_FILESYSTEM_TOOLS=true. Enables direct file read/write access to your Home Assistant filesystem. WARNING: This gives the MCP server sensitive direct file access to your system. Only enable if you trust the AI assistant with file operations. Requires restart to take effect.",
  },
  enable_custom_component_integration: {
    label: "Enable custom component integration (beta)",
    help: "Sets HAMCP_ENABLE_CUSTOM_COMPONENT_INTEGRATION=true. Enables the ha_install_mcp_tools installer tool, which can help install the ha_mcp_tools custom component. This setting does not control whether the MCP server loads or interacts with the custom component, and it is not required for filesystem tools to function. Only enable if you want to allow the AI assistant to use the installer tool. Requires restart to take effect.",
  },
  enable_code_mode: {
    label: "Enable code-mode sandbox (beta)",
    help: "Beta feature — disabled by default. Enables ha_manage_custom_tool, a sandboxed Python interpreter (pydantic-monty) that lets AI assistants write/run/save/delete custom tools when no built-in tool covers the request. Sandbox cannot touch the filesystem or arbitrary network, but CAN call any registered MCP tool, hit the HA REST API, or send HA WebSocket commands — effectively 'do whatever existing tools allow you to do, in any combination'. See docs/beta.md for known limitations. Requires restart to take effect.",
  },
  enable_lite_docstrings: {
    label: "Enable lite tool docstrings (beta)",
    help: "Beta feature — disabled by default. Replaces the docstrings on a handful of heavy ha-mcp tools (automations, scripts, scenes, helpers, dashboards, ha_call_service, ha_config_set_yaml) with shorter variants that defer schema and example detail to the ha_get_skill_guide tool (or its skill:// resource). WARNING: this reduces idle token usage, but may degrade LLM performance — the trimmed descriptions rely on the LLM actually calling the skill tool or reading the skill resource for detail, which is not guaranteed (some models will skip the extra tool call and end up with less guidance than they had before). Best paired with a client that supports MCP resources or with enable_tool_search. Requires restart to take effect.",
  },
};

// The 5 beta sub-flag fields gated by the master beta toggle. Populated
// from the ``beta_sub_flags`` array in the /api/settings/features
// response so the JS stays in sync with Python's
// ``config.BETA_FEATURE_FIELDS`` without duplicating the name list here.
let BETA_SUB_FLAGS = new Set();

// Cached add-on flag. Each settings endpoint (/api/settings/features,
// /api/settings/advanced, /api/settings/backup-config) returns
// ``is_addon`` so the env-locked banner copy can adapt — the addon
// Configuration UI cannot "unset env vars," so the standalone-mode
// "unset env var" copy is actively misleading there (#1164).
let IS_ADDON_MODE = false;

const ORIGIN_LOCKED_NOTE = {
  env: 'Set via environment variable — unset it to edit here.',
  // addon-origin fields are editable: save POSTs through Supervisor
  // /addons/self/options and triggers a restart so both surfaces stay
  // in sync. No locked note needed.
};

const ORIGIN_INFO_NOTE = {
  addon: 'Synced to the add-on Configuration tab — restart required after save.',
};

// Compose the env-locked banner text for one field. Addon-mode copy
// avoids the misleading "unset it to edit here" — operators in HA
// addon mode have no env-var surface to unset; the var was set
// either by start.py from /data/options.json or by Supervisor itself
// (and in either case the addon Configuration tab is the place to
// change it). The master `enable_beta_features` row uses different
// copy because it's now schema-bound on dev — origin='env' there
// only fires on the legacy-bridge path (a pre-#1164 install whose
// options.json doesn't carry the master key yet, where start.py's
// truthy-sub-flag fallback wrote ENABLE_BETA_FEATURES=true).
function envLockedNoteHtml(envVar, fieldName) {
  const envVarTag = `<code>${escapeHtml(envVar)}</code>`;
  if (!IS_ADDON_MODE) {
    return `Set via env var ${envVarTag} — unset it to edit here.`;
  }
  if (fieldName === 'enable_beta_features') {
    return (
      `Auto-enabled in addon mode (legacy bridge — your options.json ` +
      `predates the master toggle schema entry). Set ` +
      `<code>enable_beta_features</code> explicitly in the addon ` +
      `Configuration tab to take direct control. (env: ${envVarTag})`
    );
  }
  return (
    `Set by the addon runtime environment — managed by Home Assistant ` +
    `Supervisor; cannot be changed from this web UI. (env: ${envVarTag})`
  );
}

async function loadFeatureFlags() {
  let resp;
  try {
    resp = await fetch('./api/settings/features');
  } catch (err) {
    console.error('loadFeatureFlags fetch failed:', err);
    // Surface as a row inside the panel rather than the page status —
    // the panel is collapsible and the user can ignore this if they
    // do not care about feature flags right now.
    document.getElementById('featuresBody').innerHTML =
      '<div class="feature-row"><div class="feature-help">' +
      'Feature flags unavailable (network error reaching ' +
      '/api/settings/features).</div></div>';
    return;
  }
  if (!resp.ok) {
    document.getElementById('featuresBody').innerHTML =
      `<div class="feature-row"><div class="feature-help">` +
      `Feature flags unavailable (HTTP ${resp.status}).</div></div>`;
    return;
  }
  let data;
  try {
    data = await resp.json();
  } catch (err) {
    console.error('loadFeatureFlags JSON parse failed:', err);
    document.getElementById('featuresBody').innerHTML =
      '<div class="feature-row"><div class="feature-help">' +
      'Feature flags response was not valid JSON.</div></div>';
    return;
  }
  if (Array.isArray(data.beta_sub_flags)) {
    BETA_SUB_FLAGS = new Set(data.beta_sub_flags);
  }
  if (typeof data.is_addon === 'boolean') {
    IS_ADDON_MODE = data.is_addon;
  }
  renderFeatureFlags(data.flags || {});
}

// Cache of last-fetched flags so we can re-render synchronously when
// the user flips the master beta toggle (without round-tripping to the
// server). Server-side master-off rejection still applies on save.
let _lastFeatureFlags = {};

function renderFeatureFlags(flags) {
  _lastFeatureFlags = flags;
  const body = document.getElementById('featuresBody');
  const betaBody = document.getElementById('betaBody');
  body.innerHTML = '';
  if (betaBody) betaBody.innerHTML = '';
  // Master beta state — drives the .dimmed class on sub-rows. Read
  // from the live cache so we get the post-flip value if the user
  // just toggled the master.
  const masterOn = !!(flags.enable_beta_features && flags.enable_beta_features.value);
  // Render in the order FEATURE_META declares — gives consistent
  // grouping (Tool Search rows together, master then beta sub-rows
  // together) regardless of dict iteration order returned by the
  // server.
  Object.keys(FEATURE_META).forEach(fieldName => {
    const f = flags[fieldName];
    if (!f) return;
    const meta = FEATURE_META[fieldName];
    const isMaster = fieldName === 'enable_beta_features';
    const isBetaSub = BETA_SUB_FLAGS.has(fieldName);
    // Beta rows render into the dedicated bottom-of-panel betaBody
    // container so the dangerous block sits below the safer
    // settings. Fallback to the main body if the dedicated container
    // is missing (tests that don't include it in MIN_DOM).
    const targetBody = (isMaster || isBetaSub) && betaBody ? betaBody : body;
    const row = document.createElement('div');
    let cls = 'feature-row' + (f.editable ? '' : ' locked');
    if (isMaster) cls += ' beta-master-row';
    if (isBetaSub) cls += ' beta-sub' + (masterOn ? '' : ' dimmed');
    row.className = cls;

    const info = document.createElement('div');
    info.className = 'feature-info';
    const lockedNote = !f.editable
      ? `<div class="feature-locked-note">` +
        (f.origin === 'env'
          ? envLockedNoteHtml(f.env_var, fieldName)
          : escapeHtml(ORIGIN_LOCKED_NOTE[f.origin] || '')) +
        `</div>`
      : '';
    const infoNote = f.editable && ORIGIN_INFO_NOTE[f.origin]
      ? `<div class="feature-locked-note">` +
        `${escapeHtml(ORIGIN_INFO_NOTE[f.origin])}</div>`
      : '';
    info.innerHTML =
      `<div class="feature-name">${escapeHtml(meta.label)}</div>` +
      `<div class="feature-help">${escapeHtml(meta.help)}</div>` +
      lockedNote + infoNote;

    const control = document.createElement('div');
    control.className = 'feature-control';
    // Beta sub-flags are disabled at the input level when the master
    // is off, in addition to the .dimmed class on the row. Server-
    // side rejection (409 in _save_feature_flags) is the
    // authoritative guard; this is UX feedback.
    const lockedByMaster = isBetaSub && !masterOn;
    if (f.type === 'bool') {
      const label = document.createElement('label');
      label.className = 'switch';
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.checked = !!f.value;
      input.disabled = !f.editable || lockedByMaster;
      input.addEventListener('change', () => {
        // Master flip → re-render the panel synchronously so the
        // sub-row dimming reflects the new state immediately. The
        // save POST still proceeds in the background.
        //
        // Sub-flag VALUES are intentionally NOT flipped here. Neither
        // is the server's persisted state — the runtime gate in
        // ``_apply_feature_flag_overrides`` is the only thing that
        // forces sub-flags off when master is off, and it does so
        // without mutating the saved values. Result: turning the
        // master off then back on restores the user's prior sub-flag
        // selections automatically, which is the intended UX for an
        // opt-in beta surface.
        if (isMaster) {
          if (_lastFeatureFlags[fieldName]) {
            _lastFeatureFlags[fieldName] = {
              ..._lastFeatureFlags[fieldName],
              value: input.checked,
            };
            renderFeatureFlags(_lastFeatureFlags);
          }
        }
        saveFeatureFlag(fieldName, input.checked);
      });
      const slider = document.createElement('span');
      slider.className = 'slider';
      label.appendChild(input);
      label.appendChild(slider);
      control.appendChild(label);
    } else if (f.type === 'int') {
      const input = document.createElement('input');
      input.type = 'number';
      input.value = f.value;
      if (typeof f.min === 'number') input.min = f.min;
      if (typeof f.max === 'number') input.max = f.max;
      input.disabled = !f.editable;
      input.addEventListener('change', () => {
        const parsed = parseInt(input.value, 10);
        if (Number.isFinite(parsed)) saveFeatureFlag(fieldName, parsed);
      });
      control.appendChild(input);
    }

    row.appendChild(info);
    row.appendChild(control);
    targetBody.appendChild(row);

    // Chunk 3b — after rendering the enable_code_mode row, inject the
    // 5 code_mode_* sub-numeric rows from the advanced cache. These
    // are second-level-nested (under enable_code_mode, which is itself
    // beta-sub-nested under the master), dimmed when either the master
    // is off or code_mode itself is off. Sub-rows go into the same
    // target body as the parent so the beta block stays grouped at
    // bottom.
    if (fieldName === 'enable_code_mode') {
      const codeModeOn = !!f.value;
      renderCodeModeSubRows(targetBody, masterOn, codeModeOn);
    }
  });
}

function renderCodeModeSubRows(parentEl, masterOn, codeModeOn) {
  const cmRows = (_advancedFields || []).filter(x => x.section === 'beta_codemode');
  cmRows.forEach(f => {
    const meta = ADVANCED_FIELD_META[f.field] || { label: f.field, help: '' };
    const row = document.createElement('div');
    const lockedByGate = !masterOn || !codeModeOn;
    const dimmed = lockedByGate;
    row.className = 'feature-row codemode-sub' + (dimmed ? ' dimmed' : '');

    const info = document.createElement('div');
    info.className = 'feature-info';
    const lockedNote = f.origin === 'env'
      ? `<div class="feature-locked-note">Set via env var <code>${escapeHtml(f.env_var)}</code> — unset it to edit here.</div>`
      : '';
    info.innerHTML =
      `<div class="feature-name">${escapeHtml(meta.label)}</div>` +
      `<div class="feature-help">${escapeHtml(meta.help)}</div>` +
      lockedNote;

    const control = document.createElement('div');
    control.className = 'feature-control';
    const disabled = !f.editable || lockedByGate;
    let inputEl;
    if (f.type === 'int' || f.type === 'float') {
      inputEl = document.createElement('input');
      inputEl.type = 'number';
      inputEl.value = f.value;
      if (typeof f.min === 'number') inputEl.min = f.min;
      if (typeof f.max === 'number') inputEl.max = f.max;
      if (f.type === 'float') inputEl.step = '0.1';
    } else {
      inputEl = document.createElement('input');
      inputEl.type = 'text';
      inputEl.value = String(f.value ?? '');
    }
    inputEl.disabled = disabled;
    inputEl.dataset.advField = f.field;
    inputEl.addEventListener('change', () => {
      let v;
      if (f.type === 'int') v = parseInt(inputEl.value, 10);
      else if (f.type === 'float') v = parseFloat(inputEl.value);
      else v = inputEl.value;
      if (typeof v === 'number' && Number.isNaN(v)) return;
      _advancedDirty[f.field] = v;
      // Surface a hint that there are unsaved code-mode-numeric
      // changes — they share the Save button(s) under the Advanced
      // sections. Mirror to both top and bottom rows.
      const status = document.getElementById('advSaveStatus');
      if (status) {
        status.textContent = 'Unsaved changes — click "Save advanced settings".';
      }
      const statusTop = document.getElementById('advSaveStatusTop');
      if (statusTop) {
        statusTop.textContent =
          'Unsaved changes — click "Save advanced settings".';
      }
      const saveRow = document.getElementById('advSaveRow');
      if (saveRow) saveRow.style.display = '';
      const saveRowTop = document.getElementById('advSaveRowTop');
      if (saveRowTop) saveRowTop.style.display = '';
    });
    control.appendChild(inputEl);

    row.appendChild(info);
    row.appendChild(control);
    parentEl.appendChild(row);
  });
}

async function saveFeatureFlag(fieldName, value) {
  updateStatus('Saving server setting...');
  let resp;
  try {
    resp = await fetch('./api/settings/features', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({flags: {[fieldName]: value}}),
    });
  } catch (e) {
    updateStatus('Save failed: ' + e.message);
    return;
  }
  let data = null;
  try { data = await resp.json(); } catch (_e) {
    // On a 200 OK with truncated / non-JSON body, default to the
    // "restart needed" state so the user gets the banner — silently
    // skipping it would let them think the change took effect live
    // and they'd never restart. Only do this on resp.ok; for an
    // error response we want the HTTP status to drive the message.
    if (resp.ok) data = {restart_required: true};
  }
  if (!resp.ok) {
    let msg = `Save failed (HTTP ${resp.status})`;
    if (data?.error?.message) msg = 'Save failed: ' + data.error.message;
    updateStatus(msg);
    return;
  }
  // Unified restart flow — save persists the change but does NOT fire
  // the addon restart. The user picks when to restart by clicking the
  // global Restart Add-on button in the cross-tab restart-required
  // banner. Same UX as the Tools tab. In standalone modes the restart
  // button is hidden (no supervisor to drive it) but the banner still
  // surfaces "restart required" as guidance.
  updateStatus('Saved — restart required', true);
  if (data?.restart_required) {
    document.getElementById('restartNotice').classList.add('show');
    if (restartChannel) restartChannel.postMessage({type: 'restart-required'});
  }
}

// ===== Tool Security Policies tab =====
// Live approval routes (pending/approve/deny) are only available from
// the main server (in-process ApprovalQueue). The sidecar serves
// config GET/PUT but returns 503 for the live endpoints — the UI
// degrades to "Live approvals unavailable in this mode."
//
// The card UI keeps an in-memory mutable copy of each rule
// (policyRuleEdits[tool_name]) so the user can edit conditions /
// remember_minutes locally before pressing "Save changes" on a card,
// which then GETs current policy, replaces the rule entry, and PUTs.
// This mirrors the syncPolicyRule() flow used by the Tools-tab toggle.
let policyRuleEdits = {};

async function syncPolicyMasterToggle() {
  // The master toggle on this tab is just a UI mirror of the same
  // `enable_tool_security_policies` feature flag the Server Settings
  // tab exposes — the addon-config flag is the single source of truth.
  // We rely on loadPolicyState() to have populated policyState.enabled
  // (it fetches /api/settings/features) so the only work here is to
  // reflect that bit into the checkbox.
  await loadPolicyState();
  const cb = document.getElementById('policy-master-toggle');
  if (cb) cb.checked = !!policyState.enabled;
}

async function policyLoadConfig() {
  await syncPolicyMasterToggle();
  const errEl = document.getElementById('policy-load-error');
  if (errEl) { errEl.style.display = 'none'; errEl.textContent = ''; }
  let resp;
  try {
    resp = await fetch('./api/policy/config');
  } catch (e) {
    showPolicyLoadError('Could not reach the server: ' + e.message);
    return;
  }
  if (!resp.ok) {
    // 500 with policy_file_corrupt:true is the explicit "your
    // tool_policy.json is broken, here's how to repair" message from
    // the handler — surface it instead of silently rendering empty.
    let detail = 'HTTP ' + resp.status;
    let bodyParsed = false;
    try {
      const body = await resp.json();
      bodyParsed = true;
      if (body && body.error) detail = body.error;
      if (body && body.policy_file_corrupt) {
        detail += ' (tool_policy.json appears corrupt; edit or delete it on the addon /data volume)';
      }
    } catch (_e) { /* keep the HTTP-status fallback */ }
    if (!bodyParsed) {
      // E.g. an HTML error page from a misrouted sidecar — give the
      // operator a hint that the body itself was unparseable, not
      // just the status code.
      detail += ' (response body unparseable)';
    }
    showPolicyLoadError('Failed to load policy: ' + detail);
    return;
  }
  const p = await resp.json();
  document.getElementById('policy-wait-seconds').value = p.wait_seconds ?? 60;
  document.getElementById('policy-ttl-minutes').value = p.approval_ttl_minutes ?? 5;
  renderPolicyCards(p);
}

function showPolicyLoadError(msg) {
  const errEl = document.getElementById('policy-load-error');
  if (!errEl) return;
  errEl.style.display = '';
  errEl.textContent = msg;
}

function renderPolicyCards(policy) {
  const listEl = document.getElementById('policy-rules-list');
  const emptyEl = document.getElementById('policy-rules-empty');
  listEl.innerHTML = '';
  policyRuleEdits = {};
  const rules = (policy && policy.rules) || [];
  if (rules.length === 0) {
    emptyEl.style.display = '';
    return;
  }
  emptyEl.style.display = 'none';
  // Group rules by tool_name. The Tools-tab toggle creates exactly one
  // rule per tool; defensively handle the case where a hand-edited file
  // has multiple entries: each becomes its own card so the user can
  // see/edit them all.
  const byTool = {};
  rules.forEach((r, idx) => {
    const key = r.tool_name + '\u0000' + idx;
    byTool[key] = {tool_name: r.tool_name, rule: r, originalIndex: idx};
  });
  Object.keys(byTool).forEach(key => {
    const entry = byTool[key];
    // Deep clone the rule into the edit buffer so card-local changes
    // don't mutate the server response until "Save changes".
    const editKey = entry.tool_name;
    policyRuleEdits[editKey] = JSON.parse(JSON.stringify(entry.rule));
    listEl.appendChild(renderPolicyCard(entry.tool_name, policyRuleEdits[editKey]));
  });
}

function displayPredicate(p) {
  if (!p || !p.path) return '(invalid)';
  if (p.op === 'exists') return p.path + ' exists';
  const val = (p.value === undefined) ? 'null' : JSON.stringify(p.value);
  return p.path + ' ' + p.op + ' ' + val;
}

function renderPolicyCard(toolName, rule) {
  const card = document.createElement('div');
  card.className = 'policy-rule-card';
  card.dataset.tool = toolName;
  rule.when = rule.when || [];
  const predicateRows = rule.when.map((p, i) => (
    '<li class="policy-predicate-row" data-idx="' + i + '">' +
      '<code>' + escapeHtml(displayPredicate(p)) + '</code>' +
      '<button class="policy-edit-predicate" data-idx="' + i + '">edit</button>' +
      '<button class="policy-remove-predicate" data-idx="' + i + '">×</button>' +
    '</li>'
  )).join('');
  const emptyHint = rule.when.length === 0
    ? '<li class="policy-predicate-row"><em style="color:var(--text-secondary);font-size:0.8rem">' +
      '(no conditions — rule matches every call to this tool)</em></li>'
    : '';
  card.innerHTML =
    '<div class="policy-rule-header">' +
      '<strong>' + escapeHtml(toolName) + '</strong>' +
      '<button class="policy-rule-remove" title="Remove from policy">×</button>' +
    '</div>' +
    '<div class="policy-rule-predicates">' +
      '<label class="features-sub" style="display:block;margin-bottom:4px">' +
        'Require approval when ALL of these conditions match (no conditions = always require approval):' +
      '</label>' +
      '<ul class="policy-predicate-list">' + emptyHint + predicateRows + '</ul>' +
      '<button class="policy-add-predicate">+ Add condition</button>' +
      '<div class="policy-predicate-form" style="display:none;">' +
        '<div class="policy-form-row">' +
          '<label class="policy-form-label">Argument:</label>' +
          '<select class="policy-predicate-path-select">' +
            '<option value="">(loading...)</option>' +
          '</select>' +
          '<input type="text" class="policy-predicate-path-custom" ' +
            'placeholder="e.g. args.color_temp" style="display:none">' +
        '</div>' +
        '<div class="policy-form-row">' +
          '<label class="policy-form-label">Match when:</label>' +
          '<select class="policy-predicate-op">' +
            '<option value="exists">is present (any value)</option>' +
            '<option value="eq">equals</option>' +
            '<option value="neq">does NOT equal</option>' +
            '<option value="in">is one of</option>' +
            '<option value="not_in">is NOT one of</option>' +
            '<option value="contains">contains</option>' +
            '<option value="regex">matches regex</option>' +
            '<option value="gt">is greater than</option>' +
            '<option value="lt">is less than</option>' +
          '</select>' +
        '</div>' +
        '<div class="policy-form-row policy-value-row">' +
          '<label class="policy-form-label">Value:</label>' +
          '<span class="policy-predicate-value-slot"></span>' +
        '</div>' +
        '<div class="policy-form-row">' +
          '<button class="policy-predicate-form-save">Save condition</button>' +
          '<button class="policy-predicate-form-cancel">Cancel</button>' +
        '</div>' +
        '<div class="policy-predicate-form-error" style="display:none;"></div>' +
      '</div>' +
    '</div>' +
    '<div class="policy-rule-lifetime">' +
      '<label>Remember approval for:' +
        '<input type="number" min="0" max="1440" class="policy-remember-minutes" ' +
          'value="' + (rule.remember_minutes || 0) + '">' +
        'minutes (0 = single-shot)' +
      '</label>' +
    '</div>' +
    '<span class="policy-save-status" style="font-size:0.78rem;color:var(--text-secondary)"></span>';

  // Auto-save: every condition add/edit/remove and every remember-minutes
  // change immediately PUTs the rule to disk. No manual "Save changes"
  // button — the only signal is the small status text below the card.
  let autoSaveSeq = 0;
  const autoSave = async () => {
    const status = card.querySelector('.policy-save-status');
    const mySeq = ++autoSaveSeq;
    status.textContent = 'Saving…';
    try {
      await savePolicyRule(toolName, rule);
      // Skip the success label if a newer save started (rapid edits)
      if (mySeq === autoSaveSeq) status.textContent = 'Saved.';
    } catch (err) {
      if (mySeq === autoSaveSeq) {
        status.textContent = 'Save failed: ' + err.message;
      }
    }
  };

  // Re-render the card in place after a condition-list mutation so the
  // rows reflect the new in-memory rule object.
  const rerenderCard = () => {
    const replacement = renderPolicyCard(toolName, rule);
    card.replaceWith(replacement);
  };

  card.querySelector('.policy-rule-remove').addEventListener('click', async () => {
    if (!confirm('Remove "' + toolName + '" from the security policy?')) return;
    try {
      await removePolicyRule(toolName);
      delete policyRuleEdits[toolName];
      card.remove();
      // Refresh card list + empty state from server (also refreshes
      // Tools-tab gated state on next visit via loadPolicyState).
      await policyLoadConfig();
    } catch (err) {
      alert('Failed to remove rule: ' + err.message);
    }
  });

  // remember-minutes is a number input; debounce so typing "30" doesn't
  // fire three saves (3, 30 — or rapid arrow-key presses).
  let rmDebounce = null;
  card.querySelector('.policy-remember-minutes').addEventListener('input', (e) => {
    rule.remember_minutes = parseInt(e.target.value, 10) || 0;
    if (rmDebounce) clearTimeout(rmDebounce);
    rmDebounce = setTimeout(autoSave, 500);
  });

  const formEl = card.querySelector('.policy-predicate-form');
  const opEl = formEl.querySelector('.policy-predicate-op');
  const pathSelectEl = formEl.querySelector('.policy-predicate-path-select');
  const pathCustomEl = formEl.querySelector('.policy-predicate-path-custom');
  const valueSlotEl = formEl.querySelector('.policy-predicate-value-slot');
  const errorEl = formEl.querySelector('.policy-predicate-form-error');
  let editingIdx = -1;
  // Tool schema is fetched lazily on first form-open and cached on
  // the card so reopening the form doesn't refetch.
  let toolSchema = null;
  // value-source choice cache: { source_key: [values] }
  const valueChoiceCache = {};

  const FREE_TEXT_OPT = '__custom__';

  const currentPath = () => (
    pathSelectEl.value === FREE_TEXT_OPT
      ? pathCustomEl.value.trim()
      : pathSelectEl.value
  );

  const populatePathSelect = (selectedPath) => {
    const paths = (toolSchema && toolSchema.paths) || [];
    let html = '';
    // Wildcard: match the condition against EVERY argument of the call.
    // Always first AND default, so the form has a sensible value out of
    // the box and users never hit "argument is required" by saving an
    // empty placeholder.
    html += '<option value="args.*" ' +
      'title="Match against every argument of the call. Combine with op=equals/is one of to gate on any arg having a given value.">' +
      '(any argument)</option>';
    for (const p of paths) {
      const tip = p.description ? ' title="' + escapeHtml(p.description) + '"' : '';
      html += '<option value="' + escapeHtml(p.path) + '"' + tip + '>' +
        escapeHtml(p.label) +
        (p.required ? ' *' : '') +
        (p.type ? ' (' + escapeHtml(p.type) + ')' : '') +
        '</option>';
    }
    html += '<option value="' + FREE_TEXT_OPT + '">(other — type a path)</option>';
    pathSelectEl.innerHTML = html;

    // If the existing condition uses a path the schema doesn't know
    // about (read-only tool, free-text from earlier, removed arg),
    // drop into custom mode automatically so we don't silently clobber
    // the existing value.
    if (selectedPath) {
      const isWildcard = selectedPath === 'args.*';
      const match = paths.find(p => p.path === selectedPath);
      if (isWildcard || match) {
        pathSelectEl.value = selectedPath;
        pathCustomEl.style.display = 'none';
        pathCustomEl.value = '';
      } else {
        pathSelectEl.value = FREE_TEXT_OPT;
        pathCustomEl.style.display = '';
        pathCustomEl.value = selectedPath;
      }
    } else {
      // New condition: default to "(any argument)" so the form is
      // immediately submittable once the user fills in a value.
      pathSelectEl.value = 'args.*';
      pathCustomEl.style.display = 'none';
      pathCustomEl.value = '';
    }
  };

  // Latest value-source fetch error, surfaced as a hint under the value
  // row so the user notices when the dropdown fell back to free-text
  // because of a real failure (vs because no source is registered).
  let lastValueSourceError = null;

  const loadValueChoices = async (sourceKey) => {
    if (valueChoiceCache[sourceKey]) {
      lastValueSourceError = null;
      return valueChoiceCache[sourceKey];
    }
    try {
      const r = await fetch('./api/policy/value-source?source=' +
        encodeURIComponent(sourceKey));
      if (!r.ok) {
        lastValueSourceError = 'value-source fetch failed (HTTP ' + r.status + ') — falling back to free-text';
        return null;
      }
      const data = await r.json();
      const values = Array.isArray(data.values) ? data.values : [];
      valueChoiceCache[sourceKey] = values;
      lastValueSourceError = null;
      return values;
    } catch (e) {
      lastValueSourceError = 'value-source fetch failed (' + e.message + ') — falling back to free-text';
      return null;
    }
  };

  // Ops where leaving the value blank is meaningful UX shorthand for
  // "gate any call where this argument is present, regardless of
  // value". On save, those blank-value entries are coerced to
  // op=exists (see readValueControl + the form-save handler). Ops
  // that genuinely require a value (regex / gt / lt) stay strict.
  const VALUE_OPTIONAL_OPS = new Set(['exists', 'eq', 'neq', 'in', 'not_in', 'contains']);

  const hintForOp = (op) => {
    if (op === 'exists') {
      return 'Leave blank — this op gates on the argument being present at all, regardless of value.';
    }
    if (op === 'in' || op === 'not_in') {
      return 'Pick one or more values, or type a JSON list. Leave blank to gate on any value.';
    }
    if (op === 'regex') {
      return 'A regular expression to match the argument against.';
    }
    if (op === 'contains') {
      return 'A substring (for strings) or item (for lists). Leave blank to gate on any value.';
    }
    if (op === 'gt' || op === 'lt') {
      return 'A number to compare against.';
    }
    return 'The value the argument must equal. Leave blank to gate on any value.';
  };

  // Sequence number for renderValueControl — rapid path/op edits can
  // start several overlapping fetches; only the latest one is allowed
  // to mutate the DOM. Without this, an earlier slow fetch can land
  // after a later fast one and clobber the user's chosen control.
  let renderSeq = 0;

  // Render the value control inside valueSlotEl based on current op +
  // path. The control is always visible (even for op=exists) so users
  // can refine the rule later without re-discovering where the input
  // went.
  const renderValueControl = async (existingValue) => {
    const mySeq = ++renderSeq;
    const op = opEl.value;
    const path = currentPath();
    const pathMeta = ((toolSchema && toolSchema.paths) || [])
      .find(p => p.path === path);
    const sourceKey = (toolSchema && toolSchema.value_sources)
      ? toolSchema.value_sources[path]
      : null;
    const isMulti = (op === 'in' || op === 'not_in');
    const isSingleChoice = (op === 'eq' || op === 'neq');
    const choosable = isMulti || isSingleChoice;

    // 1) Live value source (e.g. ha_entities) wins — most useful.
    if (sourceKey && choosable) {
      if (mySeq !== renderSeq) return;
      valueSlotEl.innerHTML = '<em style="color:var(--text-secondary);font-size:0.78rem">' +
        'Loading choices…</em>';
      const choices = await loadValueChoices(sourceKey);
      if (mySeq !== renderSeq) return;  // newer render in flight; discard.
      if (choices) {
        renderChoiceSelect(choices, existingValue, isMulti);
        renderHint(op);
        return;
      }
      // fetch failed → fall through to free-text (renderHint will
      // surface the error via lastValueSourceError below).
    }

    // 2) Schema-declared enum — render as choice list too.
    if (choosable && pathMeta && Array.isArray(pathMeta.enum) && pathMeta.enum.length) {
      if (mySeq !== renderSeq) return;
      renderChoiceSelect(pathMeta.enum, existingValue, isMulti);
      renderHint(op);
      return;
    }

    // 3) Free-text JSON fallback (or op=exists, where blank is the norm).
    if (mySeq !== renderSeq) return;
    renderFreeTextValue(existingValue);
    renderHint(op);
  };

  const renderChoiceSelect = (choices, existingValue, isMulti) => {
    const existingArr = Array.isArray(existingValue)
      ? existingValue
      : (existingValue !== undefined && existingValue !== null ? [existingValue] : []);
    let html = '<select class="policy-predicate-value-control"' +
      (isMulti ? ' multiple size="6" style="min-width:220px"' : '') +
      '>';
    if (!isMulti) {
      html += '<option value="">(pick a value)</option>';
    }
    for (const c of choices) {
      const selected = existingArr.includes(c) ? ' selected' : '';
      html += '<option value="' + escapeHtml(String(c)) + '"' + selected + '>' +
        escapeHtml(String(c)) + '</option>';
    }
    html += '</select>';
    valueSlotEl.innerHTML = html;
  };

  const renderFreeTextValue = (existingValue) => {
    const op = opEl.value;
    let placeholder;
    if (op === 'exists') {
      placeholder = 'usually left blank';
    } else if (op === 'in' || op === 'not_in') {
      placeholder = '["lock","alarm_control_panel"]';
    } else if (op === 'regex') {
      placeholder = '^light\\..+';
    } else {
      placeholder = '"lock"  or  42  or  true';
    }
    const initial = (existingValue === undefined || existingValue === null)
      ? ''
      : JSON.stringify(existingValue);
    valueSlotEl.innerHTML = '<input type="text" ' +
      'class="policy-predicate-value-control policy-predicate-value" ' +
      'placeholder="' + escapeHtml(placeholder) + '" ' +
      'value="' + escapeHtml(initial) + '">';
  };

  const renderHint = (op) => {
    // Remove any previous hint then add a fresh one below the value row.
    const oldHint = formEl.querySelector('.policy-form-hint');
    if (oldHint) oldHint.remove();
    const hint = document.createElement('div');
    hint.className = 'policy-form-hint';
    let text = hintForOp(op);
    // If a value-source fetch failed (HA outage, sidecar 503, …) the
    // dropdown silently downgraded to free-text — surface that so the
    // user knows the typo'd rule they're about to author isn't picking
    // from a populated list.
    if (lastValueSourceError) {
      text = lastValueSourceError + ' — ' + text;
      hint.style.color = 'var(--danger)';
    }
    hint.textContent = text;
    formEl.querySelector('.policy-value-row').after(hint);
  };

  const readValueControl = () => {
    const op = opEl.value;
    const ctrl = valueSlotEl.querySelector('.policy-predicate-value-control');
    if (!ctrl) return {ok: true, value: undefined};
    if (ctrl.tagName === 'SELECT') {
      if (ctrl.multiple) {
        const picked = Array.from(ctrl.selectedOptions).map(o => o.value);
        if (picked.length === 0) {
          if (VALUE_OPTIONAL_OPS.has(op)) return {ok: true, value: undefined};
          return {ok: false, error: 'pick at least one value'};
        }
        return {ok: true, value: picked};
      }
      if (!ctrl.value) {
        if (VALUE_OPTIONAL_OPS.has(op)) return {ok: true, value: undefined};
        return {ok: false, error: 'pick a value'};
      }
      return {ok: true, value: ctrl.value};
    }
    const raw = ctrl.value.trim();
    if (!raw) {
      if (VALUE_OPTIONAL_OPS.has(op)) return {ok: true, value: undefined};
      return {ok: false, error: 'value is required for op=' + op};
    }
    // First try raw JSON. If that fails, fall back to smart-coercion
    // so users can type "lock" or "lock,alarm" without remembering the
    // quoting rules.
    try {
      return {ok: true, value: JSON.parse(raw)};
    } catch (_e) {
      const coerced = coerceBarewords(raw, op);
      if (coerced.ok) return coerced;
      return {ok: false, error: coerced.error};
    }
  };

  // Coerce common bareword inputs into the JSON the backend expects.
  // "lock"               (op=eq)        → "lock"
  // "lock"               (op=in)        → ["lock"]
  // "lock,alarm_control" (op=in/not_in) → ["lock","alarm_control"]
  // "42"                 → 42  (numeric autodetect for any op)
  // "true" / "false"     → boolean
  const coerceBarewords = (raw, op) => {
    const wrap = (v) => (op === 'in' || op === 'not_in') ? [v] : v;
    if (op === 'in' || op === 'not_in') {
      // Try comma-split first — if any chunk is comma-separated, build list
      if (raw.indexOf(',') !== -1) {
        const items = raw.split(',').map(s => s.trim()).filter(Boolean);
        if (items.length === 0) {
          return {ok: false, error: 'empty list for op=' + op};
        }
        return {ok: true, value: items.map(coerceScalar)};
      }
    }
    const scalar = coerceScalar(raw);
    return {ok: true, value: wrap(scalar)};
  };

  const coerceScalar = (s) => {
    if (s === 'true') return true;
    if (s === 'false') return false;
    if (s === 'null') return null;
    if (/^-?\\d+$/.test(s)) return parseInt(s, 10);
    if (/^-?\\d+\\.\\d+$/.test(s)) return parseFloat(s);
    return s; // plain string
  };

  const fetchToolSchema = async () => {
    if (toolSchema !== null) return toolSchema;
    try {
      const r = await fetch('./api/policy/tool-schema?name=' +
        encodeURIComponent(toolName));
      if (r.ok) {
        toolSchema = await r.json();
      } else {
        // 503/404/etc: server can't introspect (sidecar / tool not
        // found). Use an empty schema so the UI still works via free
        // text. Surface the failure through lastValueSourceError so
        // renderHint shows the user why their dropdown is gone.
        toolSchema = {paths: [], value_sources: {}};
        lastValueSourceError = 'tool-schema fetch failed (HTTP ' + r.status +
          ') — falling back to free-text';
      }
    } catch (e) {
      toolSchema = {paths: [], value_sources: {}};
      lastValueSourceError = 'tool-schema fetch failed (' + e.message +
        ') — falling back to free-text';
    }
    return toolSchema;
  };

  opEl.addEventListener('change', () => renderValueControl(undefined));
  pathSelectEl.addEventListener('change', () => {
    pathCustomEl.style.display = (pathSelectEl.value === FREE_TEXT_OPT) ? '' : 'none';
    renderValueControl(undefined);
  });
  pathCustomEl.addEventListener('input', () => renderValueControl(undefined));

  const openForm = async (idx) => {
    editingIdx = idx;
    errorEl.style.display = 'none';
    errorEl.textContent = '';
    formEl.style.display = '';
    await fetchToolSchema();
    if (idx >= 0) {
      const p = rule.when[idx];
      opEl.value = p.op || 'eq';
      populatePathSelect(p.path || '');
      await renderValueControl(p.value);
    } else {
      opEl.value = 'eq';
      populatePathSelect('');
      await renderValueControl(undefined);
    }
  };

  card.querySelector('.policy-add-predicate').addEventListener('click', () => openForm(-1));

  card.querySelectorAll('.policy-edit-predicate').forEach(btn => {
    btn.addEventListener('click', () => openForm(parseInt(btn.dataset.idx, 10)));
  });

  card.querySelectorAll('.policy-remove-predicate').forEach(btn => {
    btn.addEventListener('click', async () => {
      const idx = parseInt(btn.dataset.idx, 10);
      rule.when.splice(idx, 1);
      await autoSave();
      rerenderCard();
    });
  });

  formEl.querySelector('.policy-predicate-form-cancel').addEventListener('click', () => {
    formEl.style.display = 'none';
    editingIdx = -1;
  });

  formEl.querySelector('.policy-predicate-form-save').addEventListener('click', async () => {
    let op = opEl.value;
    const path = currentPath();
    if (!path) {
      errorEl.textContent = 'argument is required';
      errorEl.style.display = '';
      return;
    }
    const predicate = {path: path, op: op};
    // op=exists is presence-only — backend rejects any value field,
    // so ignore whatever's in the value box even if the user typed
    // something. Other ops read normally.
    if (op !== 'exists') {
      const parsed = readValueControl();
      if (!parsed.ok) {
        errorEl.textContent = parsed.error;
        errorEl.style.display = '';
        return;
      }
      if (parsed.value === undefined) {
        // User left value blank on an op where "any value matches"
        // is meaningful UX shorthand (eq/neq/in/not_in/contains).
        // Silently coerce to op=exists so the row reads as
        // "args.* exists" and the rule actually gates on presence
        // rather than storing a useless null-match.
        op = 'exists';
        predicate.op = 'exists';
      } else {
        predicate.value = parsed.value;
      }
    }
    if (editingIdx >= 0) {
      rule.when[editingIdx] = predicate;
    } else {
      rule.when.push(predicate);
    }
    await autoSave();
    rerenderCard();
  });

  return card;
}

async function savePolicyRule(toolName, ruleObj) {
  const r = await fetch('./api/policy/config');
  if (!r.ok) throw new Error('Could not load policy: ' + r.status);
  const policy = await r.json();
  policy.rules = policy.rules || [];
  const idx = policy.rules.findIndex(rule => rule.tool_name === toolName);
  if (idx >= 0) {
    policy.rules[idx] = ruleObj;
  } else {
    // Defensive: a card exists for a tool with no server-side rule
    // (e.g. the user removed the rule from another tab between load
    // and save). Append rather than silently drop the edit.
    policy.rules.push(ruleObj);
  }
  await policyPut(policy, 'Save rule');
}

async function removePolicyRule(toolName) {
  // Mirror syncPolicyRule(toolName, false) — kept as a separate helper
  // so the card's remove button stays self-contained, but the on-wire
  // shape is identical.
  await syncPolicyRule(toolName, false);
}

async function saveGlobalSettings() {
  const statusEl = document.getElementById('policy-global-save-status');
  statusEl.textContent = 'Saving...';
  let resp;
  try {
    resp = await fetch('./api/policy/config');
  } catch (e) {
    statusEl.textContent = 'Network error: ' + e.message;
    return;
  }
  if (!resp.ok) {
    statusEl.textContent = 'Load failed: ' + resp.status;
    return;
  }
  const policy = await resp.json();
  policy.wait_seconds = parseInt(document.getElementById('policy-wait-seconds').value, 10);
  policy.approval_ttl_minutes = parseInt(document.getElementById('policy-ttl-minutes').value, 10);
  try {
    await policyPut(policy, 'Save global settings');
    statusEl.textContent = 'Saved.';
  } catch (e) {
    statusEl.textContent = e.message;
  }
}

async function policyLoadPending() {
  const list = document.getElementById('policy-pending-list');
  let resp;
  try {
    resp = await fetch('./api/policy/pending');
  } catch (e) {
    // Surface the failure inline — silent return leaves the pending
    // list visibly frozen with no signal that polling broke.
    list.innerHTML = '<em style="color:var(--text-secondary)">Lost contact with server (' + escapeHtml(e.message) + ') — retrying.</em>';
    return;
  }
  if (resp.status === 503) {
    // 503 has three causes. Only confidently say "feature is off"
    // when /api/settings/features actually told us so; if we couldn't
    // determine the flag (network drop, server down), fall back to
    // the server's 503 message rather than misleadingly claiming the
    // user disabled the feature.
    if (policyState.enabledKnown && !policyState.enabled) {
      list.innerHTML = '<em>Tool Security Policies is turned off. Toggle it on (top of this tab or in Server Settings) and restart the addon to enable gating.</em>';
    } else {
      // Feature is on (or unknown) but the queue isn't reachable —
      // sidecar mode, startup ImportError, or transient outage.
      let msg = 'Live approvals unavailable. Check the addon log for ImportError / RuntimeError details.';
      try {
        const body = await resp.json();
        if (body && body.error) msg = body.error;
      } catch (_e) { /* keep default */ }
      list.innerHTML = '<em>' + escapeHtml(msg) + '</em>';
    }
    return;
  }
  if (!resp.ok) return;
  const data = await resp.json();
  const pending = data.pending || [];
  if (pending.length === 0) {
    list.textContent = 'No pending approvals.';
    return;
  }
  list.innerHTML = pending.map(p => (
    '<div data-pending-token="' + escapeHtml(p.token) + '" style="border:1px solid var(--border); padding:10px; margin:6px 0; border-radius:8px; background:var(--surface)">' +
    '<strong>' + escapeHtml(p.tool_name) + '</strong>' +
    '<pre style="white-space:pre-wrap; background:var(--bg); padding:8px; margin:6px 0; border-radius:6px; font-size:0.8rem">' +
    escapeHtml(JSON.stringify(p.args, null, 2)) + '</pre>' +
    '<small style="color:var(--text-secondary)">Expires: ' + escapeHtml(p.expires_at) + '</small><br>' +
    '<div style="margin-top:8px; display:flex; gap:8px">' +
    '<button class="restart-btn" data-policy-token="' + escapeHtml(p.token) + '" data-policy-action="approve">Approve</button>' +
    '<button class="danger-btn" data-policy-token="' + escapeHtml(p.token) + '" data-policy-action="deny">Deny</button>' +
    '</div></div>'
  )).join('');
  // Re-bind decision buttons each render (no event delegation needed —
  // pending list is small and re-rendered on every poll).
  list.querySelectorAll('button[data-policy-token]').forEach(btn => {
    btn.addEventListener('click', () =>
      policyDecide(btn.dataset.policyToken, btn.dataset.policyAction)
    );
  });
}

async function policyDecide(token, action) {
  let resp;
  try {
    resp = await fetch('./api/policy/' + action, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token: token}),
    });
  } catch (e) {
    alert('Network error: ' + e.message);
    return;
  }
  if (!resp.ok) {
    let body;
    try { body = await resp.json(); } catch (_) { body = {error: 'HTTP ' + resp.status}; }
    if (resp.status === 409 && body.current_decision) {
      alert("This approval was already " + body.current_decision +
            " — possibly by another tab or session.");
    } else if (resp.status === 404) {
      alert("This approval token is no longer valid (already consumed or expired).");
    } else {
      alert('Approval action failed: ' + (body.error || resp.statusText));
    }
  }
  policyLoadPending();
}

document.getElementById('policy-save-global-btn').addEventListener('click', saveGlobalSettings);

// Master toggle on this tab mirrors the Server Settings checkbox.
// Persist via the same /api/settings/features endpoint so a save here
// shows up in Server Settings (and the addon's config.yaml) on reload.
document.getElementById('policy-master-toggle').addEventListener('change', async (e) => {
  const previous = !e.target.checked;  // user just flipped; previous is the OPPOSITE.
  await saveFeatureFlag('enable_tool_security_policies', e.target.checked);
  // Re-read the truth from the server and sync the checkbox back to
  // it. If saveFeatureFlag silently failed (network drop / 5xx) the
  // server still has the old value and we need to revert the
  // checkbox so the UI doesn't lie about persisted state.
  await loadPolicyState();
  if (policyState.enabledKnown) {
    e.target.checked = !!policyState.enabled;
  } else {
    // Can't confirm what the server has — revert to the pre-flip
    // value and let the status message tell the user save failed.
    e.target.checked = previous;
  }
});

// Poll for pending approvals every 3s when Tool Security Policies tab is visible.
setInterval(() => {
  const policiesTab = document.querySelector('.tab[data-panel="tool-security-policies"]');
  if (policiesTab && policiesTab.classList.contains('active')) {
    policyLoadPending();
  }
}, 3000);

// ===== Tab switching =====
// Generic dispatcher — every .tab button names its target panel via
// data-panel, every .panel has matching id="panel-<name>". Adding a
// new tab is one button + one panel div; no JS change needed.
function activateTab(target) {
  document.querySelectorAll('.tab').forEach(t =>
    t.classList.toggle('active', t.dataset.panel === target)
  );
  document.querySelectorAll('.panel').forEach(p =>
    p.classList.toggle('active', p.id === 'panel-' + target)
  );
  if (target === 'backups') { loadBackupConfig(); loadBackups(); }
  if (target === 'tool-security-policies') { policyLoadConfig(); policyLoadPending(); }
  if (target === 'tools') {
    // Refresh gated-toggle state in case the user changed rules from
    // the Tool Security Policies tab while it was active.
    loadPolicyState().then(render).catch(() => {});
  }
}

document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => activateTab(tab.dataset.panel));
});

// Cross-tab links — any <a data-panel-link="<name>"> switches tabs
// in-page rather than following the href (used by the "no gated
// tools" empty state to point users at the Tools tab).
document.addEventListener('click', (e) => {
  const link = e.target.closest('[data-panel-link]');
  if (!link) return;
  e.preventDefault();
  activateTab(link.dataset.panelLink);
});

// ===== Advanced settings (#1164) =====
const ADVANCED_FIELD_META = {
  homeassistant_url:   { label: "Home Assistant URL",          help: "Display only — set via HOMEASSISTANT_URL env var or addon-managed (Supervisor)." },
  homeassistant_token: { label: "Home Assistant token",        help: "Display only — set via HOMEASSISTANT_TOKEN env var. Masked here for security." },
  timeout:             { label: "HA request timeout (s)",      help: "Per-request HTTP timeout. Range 1–600. Restart required." },
  max_retries:         { label: "HA request max retries",      help: "Retry budget per failed REST call. Range 0–20. Restart required." },
  verify_ssl:          { label: "Verify SSL certificates",     help: "Skip TLS verification only on trusted networks (self-signed certs, hostname mismatch). Restart required." },
  fuzzy_threshold:     { label: "Fuzzy-search threshold",      help: "Lower = looser entity match. Range 0–100." },
  entity_search_limit: { label: "Entity search result limit",  help: "Max entities returned by ha_search_entities. Range 1–1000." },
  backup_hint:         { label: "Backup-hint level",           help: "Tunes how strongly the LLM is prompted to take a full-HA snapshot before risky writes." },
  enable_websocket:    { label: "Enable WebSocket",            help: "WebSocket-based state monitoring. Disabling falls back to polling — many tools degrade. Restart required." },
  enabled_tool_modules: { label: "Enabled tool modules",       help: "Comma-separated module names, or 'all'. Restricts which tool registry modules load at startup. Restart required." },
  enable_dashboard_partial_tools: { label: "Dashboard partial-update tools", help: "Token-efficient partial dashboard tools. Disable for clients with programmatic tool use." },
  mcp_server_name:     { label: "MCP server name",             help: "Reported in MCP handshake. Restart required." },
  mcp_server_version:  { label: "MCP server version",          help: "Defaults to the package version. Overriding can confuse clients that key on this string. Restart required." },
  environment:         { label: "Environment",                 help: "'development' or 'production'. Affects logging verbosity. Restart required." },
  log_level:           { label: "Log level",                   help: "DEBUG/INFO/WARNING/ERROR/CRITICAL. Set once at startup — restart required." },
  debug:               { label: "Debug mode",                  help: "Verbose request logging. Implies sensitive data in logs — do not enable in production. Restart required." },
  code_mode_max_duration:    { label: "Code-mode max duration (s)",   help: "Wall-clock budget per sandbox run. Range 1–300. Restart required." },
  code_mode_max_memory:      { label: "Code-mode max memory (bytes)", help: "RSS cap per sandbox run. Range 1 MB–256 MB. Restart required." },
  code_mode_max_recursion:   { label: "Code-mode max recursion",      help: "Recursion-depth cap per sandbox run. Restart required." },
  code_mode_max_invocations: { label: "Code-mode max invocations",    help: "API/tool-call cap per sandbox run. Restart required." },
  code_mode_saved_tools_path:{ label: "Saved-tools path",              help: "JSON file where ha_manage_custom_tool persists saved tools across restarts. Restart required." },
};

// Fields that require an MCP-host restart to take effect when changed
// from this surface. Used to surface the restart-required banner on save.
// REST client construction (timeout / verify_ssl / max_retries) is cached
// once at startup so those need restart even though the underlying call
// is per-request.
const ADVANCED_RESTART_REQUIRED = new Set([
  "timeout", "max_retries", "verify_ssl",
  "enabled_tool_modules", "enable_websocket",
  "log_level", "debug",
  "mcp_server_name", "mcp_server_version", "environment",
  // fuzzy_threshold is read once by SmartSearchTools at the
  // lazy-init singleton (tools/smart_search.py) — changes
  // need restart to rebuild the searcher.
  "fuzzy_threshold",
  "code_mode_max_duration", "code_mode_max_memory",
  "code_mode_max_recursion", "code_mode_max_invocations",
  "code_mode_saved_tools_path",
]);

let _advancedFields = [];
let _advancedDirty = {};  // {field: newValue} for unsaved edits

async function loadAdvancedSettings() {
  // Mirrors loadFeatureFlags' 3-arm error handling: surface network /
  // HTTP / parse failures in the first section container so the user
  // (and field debuggers reading the page) can see what went wrong.
  // Console-log too so devtools has a stack.
  // Connection section was removed (#1164 follow-up); fall back to
  // advSearch — the first remaining section — for error display.
  const errSlot = document.getElementById('advSearch');
  let resp;
  try {
    resp = await fetch('./api/settings/advanced');
  } catch (err) {
    console.error('loadAdvancedSettings fetch failed:', err);
    if (errSlot) errSlot.innerHTML =
      '<div class="adv-row"><div class="adv-help">' +
      'Advanced settings unavailable (network error reaching ' +
      '/api/settings/advanced).</div></div>';
    return;
  }
  if (!resp.ok) {
    if (errSlot) errSlot.innerHTML =
      `<div class="adv-row"><div class="adv-help">` +
      `Advanced settings unavailable (HTTP ${resp.status}).</div></div>`;
    return;
  }
  let data;
  try {
    data = await resp.json();
  } catch (err) {
    console.error('loadAdvancedSettings JSON parse failed:', err);
    if (errSlot) errSlot.innerHTML =
      '<div class="adv-row"><div class="adv-help">' +
      'Advanced settings response was not valid JSON.</div></div>';
    return;
  }
  _advancedFields = data.fields || [];
  if (typeof data.is_addon === 'boolean') {
    IS_ADDON_MODE = data.is_addon;
  }
  _advancedDirty = {};
  const bySection = {};
  _advancedFields.forEach(f => {
    (bySection[f.section] ||= []).push(f);
  });
  // Render each section into its dedicated container. Sections from
  // ADVANCED_SETTINGS_FIELDS that are NOT in the Server Settings tab
  // (e.g. "beta_codemode" is rendered under the Beta master toggle by
  // Chunk 3b, not here, and "connection" was removed from the panel
  // per user feedback) are skipped at this surface — they have no
  // container in panel-server. renderAdvancedSection is a no-op when
  // its target container is missing.
  renderAdvancedSection('advSearch', bySection.search || []);
  renderAdvancedSection('advOperations', bySection.operations || []);
  renderAdvancedSection('advToolsSurface', bySection.tools_surface || []);
  renderAdvancedSection('advDiagnostics', bySection.diagnostics || []);
  document.getElementById('advSaveRow').style.display = '';
  const topRow = document.getElementById('advSaveRowTop');
  if (topRow) topRow.style.display = '';
  document.getElementById('advSaveStatus').textContent = '';
  const topStatus = document.getElementById('advSaveStatusTop');
  if (topStatus) topStatus.textContent = '';
  // Re-render feature flags so the code_mode sub-numerics show up
  // beneath enable_code_mode (race: loadFeatureFlags may have run
  // before _advancedFields was populated). Cheap no-op if feature
  // flags haven't loaded yet.
  if (Object.keys(_lastFeatureFlags).length > 0) {
    renderFeatureFlags(_lastFeatureFlags);
  }
}

function renderAdvancedSection(containerId, fields) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = '';
  fields.forEach(f => {
    const row = document.createElement('div');
    row.className = 'adv-row' + (f.editable ? '' : ' locked');
    const meta = ADVANCED_FIELD_META[f.field] || { label: f.field, help: '' };
    let controlHtml;
    if (f.choices) {
      controlHtml = `<select data-adv-field="${escapeHtml(f.field)}" ${f.editable ? '' : 'disabled'}>` +
        f.choices.map(c =>
          `<option value="${escapeHtml(c)}" ${String(f.value) === c ? 'selected' : ''}>${escapeHtml(c)}</option>`
        ).join('') +
        '</select>';
    } else if (f.type === 'bool') {
      controlHtml = `<input type="checkbox" data-adv-field="${escapeHtml(f.field)}" ${f.value ? 'checked' : ''} ${f.editable ? '' : 'disabled'}>`;
    } else if (f.type === 'int' || f.type === 'float') {
      controlHtml = `<input type="number" data-adv-field="${escapeHtml(f.field)}" value="${Number(f.value)}" ` +
        (f.min !== undefined ? `min="${f.min}" ` : '') +
        (f.max !== undefined ? `max="${f.max}" ` : '') +
        (f.type === 'float' ? 'step="0.1" ' : '') +
        (f.editable ? '' : 'disabled') + '>';
    } else {
      // str
      controlHtml = `<input type="text" data-adv-field="${escapeHtml(f.field)}" value="${escapeHtml(String(f.value ?? ''))}" ${f.editable ? '' : 'disabled'}>`;
    }
    let originMsg = '';
    if (f.origin === 'env') {
      originMsg = envLockedNoteHtml(f.env_var, f.field);
    } else if (!f.editable) {
      originMsg = 'Display only — modify via env var or addon settings.';
    }
    row.innerHTML =
      `<div class="adv-info">` +
        `<div class="adv-name">${escapeHtml(meta.label)}</div>` +
        `<div class="adv-help">${escapeHtml(meta.help)}</div>` +
        (originMsg ? `<div class="adv-locked-note">${originMsg}</div>` : '') +
      `</div>` +
      `<div class="adv-control">${controlHtml}</div>`;
    el.appendChild(row);
  });
  // Wire change handlers so we can batch unsaved edits.
  el.querySelectorAll('[data-adv-field]').forEach(input => {
    input.addEventListener('change', () => {
      const fname = input.dataset.advField;
      const f = _advancedFields.find(x => x.field === fname);
      if (!f) return;
      let v;
      if (input.type === 'checkbox') v = input.checked;
      else if (input.type === 'number') v = (f.type === 'float') ? parseFloat(input.value) : parseInt(input.value, 10);
      else v = input.value;
      _advancedDirty[fname] = v;
    });
  });
}

// Top + bottom save buttons share state — the user can hit either,
// status text mirrors to both so the one they're looking at always
// reflects the latest outcome (#1164 follow-up).
function _advSaveBtns() {
  return [
    document.getElementById('advSaveBtn'),
    document.getElementById('advSaveBtnTop'),
  ].filter(Boolean);
}
function _advSaveStatusEls() {
  return [
    document.getElementById('advSaveStatus'),
    document.getElementById('advSaveStatusTop'),
  ].filter(Boolean);
}
function _setAdvSaveStatus(text) {
  _advSaveStatusEls().forEach(el => { el.textContent = text; });
}
function _setAdvSaveDisabled(disabled) {
  _advSaveBtns().forEach(b => { b.disabled = disabled; });
}

async function saveAdvancedSettings() {
  const btns = _advSaveBtns();
  if (!btns.length) {
    console.error('saveAdvancedSettings: no save buttons in DOM');
    return;
  }
  if (Object.keys(_advancedDirty).length === 0) {
    // Feature-flag toggles (master beta, Tool Search, etc.) auto-save
    // on click via ``saveFeatureFlag`` — they don't pass through
    // ``_advancedDirty``. If a feature-flag save just landed,
    // ``restartNotice`` is showing and the user should click Restart,
    // not Save again. Tell them that explicitly so the big Save
    // button doesn't look broken when they were toggling beta flags
    // (#1164 follow-up).
    const restartNotice = document.getElementById('restartNotice');
    const restartShowing =
      restartNotice && restartNotice.classList.contains('show');
    if (restartShowing) {
      _setAdvSaveStatus(
        'No advanced changes to save — your feature-flag toggles already ' +
        'saved on click. Click Restart above to apply them.'
      );
    } else {
      _setAdvSaveStatus('Nothing to save.');
    }
    return;
  }
  _setAdvSaveDisabled(true);
  _setAdvSaveStatus('Saving…');
  // Partition the dirty fields into addon-routed and file-routed
  // batches (#1164 follow-up). The server rejects mixed batches with
  // 500 so the UI splits them client-side: addon-synced fields go in
  // their own POST (routes through Supervisor /addons/self/options),
  // file-mode fields go in a separate POST (writes the override file).
  // Both batches must succeed for the save to count.
  const addonDirty = {};
  const fileDirty = {};
  Object.entries(_advancedDirty).forEach(([fname, val]) => {
    const f = _advancedFields.find(x => x.field === fname);
    if (f && f.origin === 'addon') {
      addonDirty[fname] = val;
    } else {
      fileDirty[fname] = val;
    }
  });
  const batches = [];
  if (Object.keys(fileDirty).length) batches.push(fileDirty);
  if (Object.keys(addonDirty).length) batches.push(addonDirty);
  const restartFields = Object.keys(_advancedDirty);
  try {
    for (const payload of batches) {
      const resp = await fetch('./api/settings/advanced', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      // JSON parse can fail on a 200 with mangled body (proxy
      // injection, truncated response). Default to
      // ``{restart_required: true}`` on success-with-garbage so the
      // user still gets the restart banner; surface "save returned
      // non-JSON" on non-OK.
      let data;
      try {
        data = await resp.json();
      } catch (parseErr) {
        console.error('saveAdvancedSettings JSON parse failed:', parseErr);
        if (resp.ok) {
          data = {restart_required: true};
        } else {
          _setAdvSaveDisabled(false);
          _setAdvSaveStatus(`Save failed (HTTP ${resp.status}, non-JSON body)`);
          return;
        }
      }
      if (!resp.ok) {
        _setAdvSaveDisabled(false);
        let msg = 'Save failed';
        if (data && data.error) {
          if (typeof data.error === 'string') msg = data.error;
          else if (data.error.message) msg = data.error.message;
        }
        _setAdvSaveStatus(msg);
        return;
      }
    }
    _setAdvSaveDisabled(false);
    _setAdvSaveStatus('Saved.');
    const needsRestart = restartFields.some(
      f => ADVANCED_RESTART_REQUIRED.has(f)
    );
    if (needsRestart) {
      document.getElementById('restartNotice').classList.add('show');
      if (typeof restartChannel !== 'undefined' && restartChannel) {
        restartChannel.postMessage({type: 'restart-required'});
      }
    }
    _advancedDirty = {};
    // Refresh display so origins update (default → file, etc.). Await
    // so a reload failure surfaces in the same status line as the save
    // — otherwise the user sees "Saved." while the panel silently
    // reverts to stale data.
    try {
      await loadAdvancedSettings();
    } catch (reloadErr) {
      console.error('post-save reload failed:', reloadErr);
      _setAdvSaveStatus('Saved (reload failed — refresh to verify).');
    }
  } catch (err) {
    _setAdvSaveDisabled(false);
    _setAdvSaveStatus('Network error: ' + String(err));
  }
}

document.getElementById('advSaveBtn').addEventListener('click', saveAdvancedSettings);
{
  const topBtn = document.getElementById('advSaveBtnTop');
  if (topBtn) topBtn.addEventListener('click', saveAdvancedSettings);
}

loadFeatureFlags();
loadAdvancedSettings();
loadTools();

// Auto-activate tab from ?tab=<name> query string (used by approval URLs
// generated by the policy middleware: /settings?tab=tool-security-policies&token=...).
// If a &token=X is present and the target is the policy tab, scroll to
// the matching pending entry once policyLoadPending() resolves.
(function activateTabFromQuery() {
  try {
    const params = new URLSearchParams(window.location.search);
    const target = params.get('tab');
    if (!target) return;
    const tabBtn = document.querySelector('.tab[data-panel="' + target + '"]');
    if (!tabBtn) return;
    activateTab(target);
    const token = params.get('token');
    if (token && target === 'tool-security-policies') {
      // policyLoadPending() runs inside activateTab; wait a tick then
      // scroll to the matching pending entry if it exists.
      setTimeout(() => {
        const row = document.querySelector('[data-pending-token="' + token + '"]');
        if (row && row.scrollIntoView) {
          row.scrollIntoView({behavior: 'smooth', block: 'center'});
        }
      }, 500);
    }
  } catch (_) { /* best-effort */ }
})();
</script>
</body>
</html>
"""
)


def _build_stub_policy_handlers(*, data_dir: Path) -> dict[str, Any]:
    """Sidecar variant of the tool security policies handlers.

    Serves policy config GET/PUT (the on-disk policy file is shared with
    the main server), but returns 503 for pending/approve/deny — those
    routes touch the in-memory ``ApprovalQueue`` which only exists in
    the main server process.
    """
    from pydantic import ValidationError

    from .policy.model import Policy
    from .policy.persistence import load_policy, save_policy

    async def get_config(_: Request) -> JSONResponse:
        try:
            return JSONResponse(load_policy(data_dir).model_dump(mode="json"))
        except ValueError as e:
            # Mirror the main-server handler: surface corruption rather
            # than crash the sidecar tab on a 500.
            return JSONResponse(
                {"error": str(e), "policy_file_corrupt": True},
                status_code=500,
            )

    async def put_config(request: Request) -> JSONResponse:
        try:
            new_policy = Policy.model_validate(await request.json())
        except (ValidationError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        # Mirror main-server optimistic concurrency: reject if on-disk
        # version moved between this caller's GET and PUT.
        current = load_policy(data_dir)
        if new_policy.version != current.version:
            return JSONResponse(
                {
                    "error": "policy version mismatch — reload before saving",
                    "current_version": current.version,
                    "current_policy": current.model_dump(mode="json"),
                },
                status_code=409,
            )
        save_policy(data_dir, new_policy)
        return JSONResponse({"saved": True, "version": new_policy.version + 1})

    async def unavailable(_: Request) -> JSONResponse:
        # 503 fires in two distinct situations:
        #   1. The Tool Security Policies feature is turned off in
        #      addon config — the middleware never registered, so no
        #      approval queue exists.
        #   2. The settings UI is running via the stdio sidecar — the
        #      in-memory queue lives in the main server process which
        #      isn't reachable from the sidecar.
        # Either way, point users at the addon log for the real reason
        # (a startup ImportError on the policy package surfaces here as
        # the same 503 with a "ModuleNotFoundError" in the log).
        return JSONResponse(
            {
                "error": (
                    "Tool security policies live approvals are not active. "
                    "Either the feature is turned off in addon config, the "
                    "settings UI is running in stdio-sidecar mode, or the "
                    "policy package failed to import at startup. Check the "
                    "addon log for ImportError / RuntimeError details if you "
                    "expected gating to be on."
                )
            },
            status_code=503,
        )

    return {
        "policy_get_config": get_config,
        "policy_put_config": put_config,
        "policy_get_pending": unavailable,
        "policy_post_approve": unavailable,
        "policy_post_deny": unavailable,
        "policy_get_tool_schema": unavailable,
        "policy_get_value_source": unavailable,
    }


class _SupervisorOptionsError(NamedTuple):
    """Discriminated failure shape for the supervisor options helpers.

    Two distinct failure classes need different recovery paths in the UI:

    - ``kind="transport"``: network / DNS / Supervisor unreachable / token
      missing. The route maps this to :class:`ErrorCode.CONNECTION_FAILED`
      so the UI surfaces the "is HA running, check connectivity"
      suggestions. ``status_code`` is always ``502`` for this kind.
    - ``kind="validation"``: Supervisor accepted the request but rejected
      the body against the addon schema (e.g. an unknown key, a missing
      required field). The route maps this to
      :class:`ErrorCode.CONFIG_VALIDATION_FAILED` and forwards the
      supervisor ``status_code`` verbatim so the UI shows a real 4xx and
      surfaces the schema-recovery suggestions.

    Collapsing both into a single string return (the previous shape)
    sent transport failures down the wrong recovery path. See
    PR #1420's code review for the motivation.
    """

    kind: Literal["transport", "validation"]
    message: str
    status_code: int

    @classmethod
    def transport(cls, message: str) -> _SupervisorOptionsError:
        """Build a transport-class error (always HTTP 502 upstream)."""
        return cls(kind="transport", message=message, status_code=502)

    @classmethod
    def validation(cls, message: str, status_code: int) -> _SupervisorOptionsError:
        """Build a validation-class error preserving supervisor's status code."""
        return cls(kind="validation", message=message, status_code=status_code)


async def _supervisor_fetch_current_options(
    verify_ssl: bool,
) -> tuple[dict[str, Any], _SupervisorOptionsError | None]:
    """GET ``/addons/self/info`` and return the current options dict.

    Supervisor's ``/addons/self/options`` POST is a *full* replacement
    validated against the addon schema — every required key must be
    present in the body. We can't ship a partial PATCH of just the
    fields the user changed, so callers must merge their changes into
    the full current options before posting. Mirrors the pattern in
    ``homeassistant-addon/start.py::maybe_persist_secret_path`` which
    spreads existing config (``{**config, "secret_path": secret_path}``)
    before calling ``persist_addon_options``.

    Returns ``(options_dict, error)`` where ``error`` is a
    :class:`_SupervisorOptionsError` carrying ``kind="transport"`` for
    network / token / non-JSON failures (mapped to ``CONNECTION_FAILED``
    upstream) and ``kind="validation"`` for supervisor ``>=400``
    responses (mapped to ``CONFIG_VALIDATION_FAILED`` with supervisor's
    real status code preserved). On success the dict carries the
    full options and ``error`` is ``None``.
    """
    try:
        async with make_supervisor_httpx_client(
            timeout=10.0, verify=verify_ssl
        ) as sclient:
            resp = await sclient.get("/addons/self/info")
    except RuntimeError as exc:
        # `make_supervisor_httpx_client` raises RuntimeError when
        # SUPERVISOR_TOKEN is unset. Both current callers gate on that
        # env var, but treat this as transport (env / setup failure) so
        # a future third caller missing the gate gets a sane 502 rather
        # than an uncaught 500.
        return {}, _SupervisorOptionsError.transport(
            f"Supervisor client unavailable: {exc}"
        )
    except httpx.HTTPError as exc:
        return {}, _SupervisorOptionsError.transport(
            f"Could not reach Supervisor for current options: {exc}"
        )
    if resp.status_code >= 400:
        # Supervisor returning a 4xx/5xx for /info is itself a transport-
        # class failure (we never sent body — there is no schema for
        # the GET to validate). 502 with CONNECTION_FAILED is right.
        return {}, _SupervisorOptionsError.transport(
            f"Supervisor returned {resp.status_code} for "
            f"/addons/self/info: {resp.text[:300]}"
        )
    try:
        body = resp.json()
    except ValueError:
        return {}, _SupervisorOptionsError.transport(
            "Supervisor returned non-JSON for /addons/self/info"
        )
    # Supervisor REST envelope is {"result": "ok", "data": {...}}. Older
    # mocks / variants may return the data dict directly — handle both.
    data = body.get("data") if isinstance(body, dict) and "data" in body else body
    if not isinstance(data, dict):
        return {}, _SupervisorOptionsError.transport(
            "Supervisor /addons/self/info had non-object body"
        )
    options = data.get("options")
    if not isinstance(options, dict):
        return {}, _SupervisorOptionsError.transport(
            "Supervisor /addons/self/info had no options dict"
        )
    return options, None


async def _supervisor_merge_and_post_options(
    verify_ssl: bool, field_changes: dict[str, Any]
) -> tuple[bool, _SupervisorOptionsError | None]:
    """Merge ``field_changes`` into supervisor's current options and POST.

    Necessary because supervisor's POST is full-replacement (see
    :func:`_supervisor_fetch_current_options`). Without this merge, a
    POST that only includes a handful of fields the user edited
    drops every other key (including required ones like ``backup_hint``)
    and supervisor rejects with a 400 ``addon_configuration_invalid_error``.

    Returns ``(success, error)`` where ``error`` is a
    :class:`_SupervisorOptionsError`. Transport failures (token missing,
    network drop, malformed response from /info) bubble up from the
    fetch helper unchanged. Supervisor 4xx on the actual POST is
    classified as ``kind="validation"`` with supervisor's status code
    preserved so the UI can show the real 4xx code and the
    ``CONFIG_VALIDATION_FAILED`` recovery suggestions.
    """
    current, err = await _supervisor_fetch_current_options(verify_ssl)
    if err is not None:
        return False, err
    merged = {**current, **field_changes}
    try:
        async with make_supervisor_httpx_client(
            timeout=10.0, verify=verify_ssl
        ) as sclient:
            resp = await sclient.post("/addons/self/options", json={"options": merged})
    except RuntimeError as exc:
        return False, _SupervisorOptionsError.transport(
            f"Supervisor client unavailable: {exc}"
        )
    except httpx.HTTPError as exc:
        return False, _SupervisorOptionsError.transport(
            f"Supervisor options POST failed: {exc}"
        )
    if resp.status_code >= 400:
        return False, _SupervisorOptionsError.validation(
            (
                f"Supervisor rejected options update ({resp.status_code}): "
                f"{resp.text[:400]}"
            ),
            resp.status_code,
        )
    return True, None


# Strong references to in-flight self-restart tasks, kept here so the
# event loop's weakref-only task table doesn't garbage-collect a still-
# running fire-and-forget coroutine before it can POST to supervisor.
# Tasks remove themselves via ``add_done_callback`` when they finish.
_BACKGROUND_RESTART_TASKS: set[asyncio.Task[None]] = set()

# Serialises read-modify-write on the shared override file
# (``feature_flags.json``) so two concurrent saves can't interleave
# their reads and clobber each other's persisted state (#1164
# follow-up). Both ``_save_feature_flags`` and ``_save_advanced_settings``
# touch the same file; without this lock, request A reading before
# request B's ``os.replace`` lands would write back a merged dict that
# misses B's changes. The runtime master gate kept functionality
# correct even when this raced, but the persisted state lied about the
# user's intent — surfacing as "I set the flag and it came back off"
# after a restart.
_OVERRIDE_FILE_LOCK: asyncio.Lock | None = None


def _get_override_file_lock() -> asyncio.Lock:
    """Lazy lock construction — ``asyncio.Lock()`` at module load
    binds the event loop that's current AT IMPORT, which doesn't exist
    yet for handlers invoked under uvicorn/starlette. Construct on
    first use under the live loop instead.
    """
    global _OVERRIDE_FILE_LOCK
    if _OVERRIDE_FILE_LOCK is None:
        _OVERRIDE_FILE_LOCK = asyncio.Lock()
    return _OVERRIDE_FILE_LOCK


# Delay (seconds) before the background self-restart task fires the
# supervisor POST. Picked to give Starlette + uvicorn time to serialize
# the JSONResponse onto the socket and have HA ingress flush it to the
# browser BEFORE supervisor kills the addon container. Too short races
# the response flush (browser sees a 5xx Bad Gateway from ingress); too
# long delays the visible restart noticeably. 0.3s is comfortably above
# observed flush times in addon-mode while staying well under any
# reasonable user attention threshold. Tests override via the ``delay``
# kwarg of ``_schedule_supervisor_self_restart``.
_SUPERVISOR_SELF_RESTART_FLUSH_DELAY_S: float = 0.3


def _schedule_supervisor_self_restart(
    verify_ssl: bool, *, delay: float = _SUPERVISOR_SELF_RESTART_FLUSH_DELAY_S
) -> None:
    """Schedule a background ``/addons/self/restart`` POST.

    Fire-and-forget on the current event loop so the request handler
    can return its JSON response *before* the supervisor kills the
    addon. Without the gap, supervisor restarts our process mid-response
    and the HA ingress proxy converts the dropped upstream connection
    into a 5xx Bad Gateway, which the browser interprets as "Restart
    failed" even though the restart actually succeeded.

    The ``delay`` (default 0.3s) gives Starlette + uvicorn time to
    serialize the JSONResponse onto the socket and have ingress flush
    it to the browser before the background coroutine wakes up and
    POSTs the supervisor restart. Tuned conservatively — too short
    races the response flush; too long delays the user-visible
    restart noticeably.

    Errors are logged and swallowed: by the time this fires the
    response has already gone out and the user has already been told
    the restart is initiated, so there is no path to surface a late
    failure here. The user discovers a failed restart by the addon
    not actually restarting; the supervisor log captures the cause.
    """

    async def _do_restart() -> None:
        await asyncio.sleep(delay)
        try:
            async with make_supervisor_httpx_client(
                timeout=5.0, verify=verify_ssl
            ) as sclient:
                resp = await sclient.post("/addons/self/restart")
            if resp.status_code >= 400:
                logger.error(
                    "Background self-restart returned %d: %s",
                    resp.status_code,
                    resp.text[:500],
                )
        except (httpx.ReadError, httpx.RemoteProtocolError):
            # Supervisor killed us mid-call — expected; no action needed.
            pass
        except RuntimeError:
            # ``make_supervisor_httpx_client`` raises RuntimeError when
            # SUPERVISOR_TOKEN is unset. The route guard at handler entry
            # already checks for this, but a race that unsets the token
            # between request entry and the 300ms-later task wakeup
            # would otherwise propagate uncaught and surface only as
            # asyncio's "Task exception was never retrieved" at GC time.
            # Log it loudly so the user can find it in the addon log.
            # Mirrors the same RuntimeError catch in the supervisor
            # options helpers.
            logger.exception("Background self-restart aborted: SUPERVISOR_TOKEN unset")
        except httpx.HTTPError:
            logger.exception("Background self-restart failed")

    task = asyncio.create_task(_do_restart())
    _BACKGROUND_RESTART_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_RESTART_TASKS.discard)


def build_settings_handlers(
    server: HomeAssistantSmartMCPServer | None,
    *,
    is_sidecar: bool = False,
) -> dict[str, Any]:
    """Construct the settings UI route handlers.

    When ``server`` is provided (HTTP modes), the tools list and restart
    handler use the live FastMCP server / Supervisor client. When
    ``server`` is ``None`` (stdio sidecar process, which has no live MCP
    server), the tools list is read from the on-disk metadata cache and
    the restart handler returns 400 (the sidecar is not an add-on).

    ``is_sidecar`` forces the ``settings_info`` handler to report
    ``is_addon=False`` regardless of the inherited ``SUPERVISOR_TOKEN``
    env var. The sidecar process inherits parent env unchanged
    (``subprocess.Popen`` with default ``env=None``), so if the parent
    stdio process happens to run under Supervisor (e.g. an interactive
    debug shell inside the add-on container) the served HTML would
    otherwise show the "Restart Add-on" button that POSTs to a route
    the sidecar doesn't expose, surfacing as a broken UI. The sidecar
    is *by construction* not the add-on entrypoint — pin the flag
    accordingly.

    Returns a dict mapping handler names to async Starlette handlers.
    Both ``register_settings_routes`` (FastMCP mounting) and the stdio
    sidecar's standalone Starlette app consume the same set of handlers
    so the served page is identical regardless of transport.
    """

    async def _root_page(_: Request) -> HTMLResponse:
        return HTMLResponse(_SETTINGS_HTML)

    async def _settings_page(_: Request) -> HTMLResponse:
        return HTMLResponse(_SETTINGS_HTML)

    async def _get_tools(_: Request) -> JSONResponse:
        if server is not None:
            tools = await _get_tool_metadata(server)
        else:
            tools = load_tool_metadata_cache()
            if not tools:
                # The sidecar's main failure mode (and the most common
                # reason a user lands on a perpetually-loading settings
                # page) is that the parent stdio process didn't write
                # the metadata cache before the sidecar served its
                # first tools request. Log loudly to the sidecar log
                # so post-mortem is one ``cat ~/.ha-mcp/sidecar.log``
                # away. The JS shows a matching diagnostic to the user.
                logger.warning(
                    "tool metadata cache is empty or missing at %s — "
                    "the parent stdio process likely did not dump it. "
                    "Check the MCP-client log for 'Failed to dump tool "
                    "metadata cache' from ha_mcp.__main__.",
                    _get_tool_metadata_cache_path(),
                )
        config = effective_tool_config()
        states = config.get("tools", {})
        pinned = env_pinned_tools()
        for name in DEFAULT_PINNED_TOOLS:
            if name not in states:
                states[name] = "pinned"
        return JSONResponse({"tools": tools, "states": states, "env_pinned": pinned})

    async def _save_tools(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except (ValueError, TypeError):
            return JSONResponse(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_JSON,
                    "Invalid JSON body",
                    suggestions=["Ensure the request body is valid JSON"],
                ),
                status_code=400,
            )

        # A valid-JSON-but-non-object payload (`null`, `[]`, `42`, `"x"`)
        # would otherwise blow up on body.get below as a 500 Internal
        # Server Error — convert to a structured 400 instead.
        if not isinstance(body, dict):
            return JSONResponse(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Request body must be a JSON object",
                ),
                status_code=400,
            )

        raw_states = body.get("states", {})
        if not isinstance(raw_states, dict):
            return JSONResponse(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "'states' must be an object mapping tool names to state values",
                ),
                status_code=400,
            )
        # Validate: keys must be strings, values must be one of the valid states
        states: dict[str, str] = {}
        for name, state in raw_states.items():
            if not isinstance(name, str) or not isinstance(state, str):
                continue
            if state not in _VALID_STATES:
                continue
            states[name] = state

        # Reject attempts to flip env-pinned tools. DISABLED_TOOLS /
        # PINNED_TOOLS are operator-level constraints that cannot be
        # overridden via the UI; callers must unset the env var first.
        # Accept no-op re-sends (state matches the env-pinned value)
        # so the periodic save fired by ``saveConfig`` after every UI
        # change doesn't 409 just because the GET payload echoed
        # env-pinned rows back unchanged (#1164 follow-up — previously
        # every save with DISABLED_TOOLS / PINNED_TOOLS non-empty
        # failed because the JS POSTs the whole ``toolStates`` map).
        env_pinned = env_pinned_tools()
        rejected = [
            name
            for name, state in states.items()
            if name in env_pinned and env_pinned[name] != state
        ]
        if rejected:
            return JSONResponse(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Refusing to flip env-pinned tools: {', '.join(rejected)}. "
                    "Unset DISABLED_TOOLS / PINNED_TOOLS first.",
                    context={"rejected": rejected},
                ),
                status_code=409,
            )
        # Drop env-pinned entries from the persisted file so the env
        # vars stay the single source of truth — preserving them in
        # tool_config.json would let a future env-var unset leave the
        # old env-pinned values mis-applied as user-set state.
        states = {
            name: state for name, state in states.items() if name not in env_pinned
        }

        config = load_tool_config()
        config["tools"] = states
        if not save_tool_config(config):
            return JSONResponse(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    "Failed to persist tool config to disk",
                    suggestions=[
                        "Set HA_MCP_CONFIG_DIR to a writable path (read-only filesystem?)",
                        "Check the server logs for the underlying OSError",
                    ],
                ),
                status_code=500,
            )

        disabled_count = sum(1 for s in states.values() if s == "disabled")
        pinned_count = sum(1 for s in states.values() if s == "pinned")
        logger.info(
            "Saved tool config (restart required to apply): %d disabled, %d pinned",
            disabled_count,
            pinned_count,
        )

        # Same response shape as ``_save_feature_flags`` and
        # ``_save_backup_config``: every save endpoint returns
        # ``{success, applied, mode, restart_required}`` so the JS can
        # branch on a single field and BroadcastChannel listeners in
        # other tabs can react uniformly. Tool config writes only ever
        # land in the on-disk JSON (no Supervisor round-trip), hence
        # ``mode="file"`` regardless of addon/standalone deployment.
        return JSONResponse(
            {
                "success": True,
                "applied": states,
                "mode": "file",
                "restart_required": True,
            }
        )

    async def _restart_addon(request: Request) -> JSONResponse:
        # The sidecar process (server is None) has no Supervisor context
        # and no live server settings — refuse cleanly. The HTTP modes
        # that do pass a server still go through the SUPERVISOR_TOKEN
        # check below, preserving the original behavior.
        if server is None or not os.environ.get("SUPERVISOR_TOKEN"):
            return JSONResponse(
                create_error_response(
                    ErrorCode.CONFIG_VALIDATION_FAILED,
                    "Restart only available when running as an add-on",
                    details="SUPERVISOR_TOKEN environment variable is not set",
                ),
                status_code=400,
            )
        # Optional slug from the request body lets callers restart a sibling
        # addon instead of self. The UI's restart button posts an empty body
        # and gets the historical self-restart behavior; the inaddon E2E
        # suite uses ``slug`` to exercise the Supervisor restart wire
        # contract against a non-test-critical addon (the dev addon's
        # session would otherwise drop). The token's hassio_role gates
        # whether the call actually succeeds for non-self targets.
        #
        # The slug is interpolated into the Supervisor endpoint URL, so it
        # must be tightly constrained — Supervisor addon slugs are
        # ``[a-z0-9_]+`` per the addon-config schema, but defending against
        # path-traversal (``..``, ``/``, URL-encoded variants) at the edge
        # is cheaper than relying on Supervisor to reject every bad shape.
        # Reject anything outside ``[A-Za-z0-9_-]`` and silently fall back
        # to ``self`` — same outcome as no body.
        target_slug = "self"
        try:
            payload = await request.json()
        except (ValueError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            requested = payload.get("slug")
            if (
                isinstance(requested, str)
                and requested.strip()
                and all(c.isalnum() or c in "_-" for c in requested.strip())
            ):
                target_slug = requested.strip()

        # Self-restart races the response flush: supervisor kills the addon
        # mid-response, ingress sees the upstream drop, and converts it into
        # a 5xx Bad Gateway at the browser — which the JS shows as
        # "Restart failed" even though the restart actually succeeded.
        # Schedule the supervisor POST from a background task so the JSON
        # response below flushes BEFORE supervisor can kill us.
        #
        # Non-self slugs target a sibling addon: the inaddon E2E suite
        # exercises that path against a non-test-critical addon, and we
        # want the supervisor response (including 4xx) to surface
        # synchronously so the test can assert on it. Keep the original
        # synchronous behavior for that path.
        if target_slug == "self":
            _schedule_supervisor_self_restart(server.settings.verify_ssl)
            return JSONResponse({"success": True, "message": "Restart initiated"})

        endpoint = f"/addons/{target_slug}/restart"
        try:
            async with make_supervisor_httpx_client(
                timeout=5.0, verify=server.settings.verify_ssl
            ) as client:
                resp = await client.post(endpoint)
        except (httpx.ReadError, httpx.RemoteProtocolError):
            # Connection dropped mid-request — restart is happening.
            # `ConnectError` is deliberately NOT in this tuple: it fires
            # before a connection is established (DNS failure, TCP refused,
            # Supervisor socket misconfigured) and means the restart was
            # never initiated. Falls through to the `httpx.HTTPError`
            # handler below, which returns 502 + CONNECTION_FAILED.
            logger.info(
                "Restart request connection dropped (expected during self-restart)"
            )
            return JSONResponse({"success": True, "message": "Restart initiated"})
        except httpx.HTTPError as e:
            logger.exception("Failed to reach Supervisor for restart")
            return JSONResponse(
                create_error_response(
                    ErrorCode.CONNECTION_FAILED,
                    f"Failed to reach Supervisor: {e}",
                ),
                status_code=502,
            )

        if resp.status_code >= 400:
            body = resp.text
            logger.error(
                "Supervisor restart failed (slug=%s): %d %s",
                target_slug,
                resp.status_code,
                body,
            )
            return JSONResponse(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    f"Supervisor returned {resp.status_code}: {body[:500]}",
                ),
                status_code=502,
            )
        return JSONResponse({"success": True, "message": "Restart initiated"})

    async def _settings_info(_: Request) -> JSONResponse:
        # Sidecar is never the add-on entrypoint regardless of inherited
        # SUPERVISOR_TOKEN — see docstring above for the broken-button
        # rationale. ``is_sidecar`` flag drives the in-page Stop Sidecar
        # button (HTML show/hide); it MUST NOT leak True for HTTP modes
        # since stopping the FastMCP-mounted route would mean killing
        # the MCP server itself.
        #
        # ``instance_id`` + ``started_at`` are surfaced so the
        # restart-then-reload JS cycle can prove a restart actually
        # happened (the value flips across processes) instead of
        # trusting that a poll returning 200 means the new instance
        # is up — a silent restart no-op would otherwise see the OLD
        # instance still answering and the page would reload to the
        # same state.
        addon = False if is_sidecar else is_running_in_addon()
        return JSONResponse(
            {
                "is_addon": addon,
                "is_sidecar": is_sidecar,
                "instance_id": _PROCESS_INSTANCE_ID,
                "started_at": _PROCESS_STARTED_AT,
            }
        )

    async def _get_feature_flags(_: Request) -> JSONResponse:
        """Return live feature-flag values + per-field origin + editable flag.

        Per-field origin/editable matrix (see
        :func:`config.get_feature_flag_origin`):

        - ``"addon"``: editable — POST routes through Supervisor
          ``/addons/self/options`` and triggers a restart so
          ``start.py`` writes the new env vars at next boot. The
          add-on Configuration tab and this web UI now share state
          bidirectionally; changing a toggle in either surface is
          reflected in the other after the addon restart.
        - ``"env"``: read-only — env var explicitly set wins; user
          must unset it to edit here.
        - ``"file"`` / ``"default"``: editable — POST writes the
          override file in place.

        The envelope shape (``flags`` dict of
        ``{value, origin, editable, type, env_var, min?, max?}``
        entries) is intentionally generic so other settings surfaces
        can render rows with the same JS code.
        """
        from .config import (
            _FEATURE_FLAG_INT_BOUNDS,
            FEATURE_FLAG_FIELDS,
            get_feature_flag_origin,
            get_global_settings,
        )

        settings = get_global_settings()
        flags: dict[str, Any] = {}
        for field_name, env_name, ftype in FEATURE_FLAG_FIELDS:
            origin = get_feature_flag_origin(env_name)
            value = getattr(settings, field_name)
            entry: dict[str, Any] = {
                "value": value,
                "origin": origin,
                "editable": origin in ("addon", "file", "default"),
                "type": ftype.__name__,
                "env_var": env_name,
            }
            if ftype is int:
                bounds = _FEATURE_FLAG_INT_BOUNDS.get(field_name)
                if bounds is not None:
                    entry["min"], entry["max"] = bounds
            flags[field_name] = entry
        from .config import BETA_FEATURE_FIELDS

        return JSONResponse(
            {
                "flags": flags,
                "beta_sub_flags": list(BETA_FEATURE_FIELDS),
                # Drives addon-aware locked-banner copy in the JS —
                # "unset env var" is misleading where HA Supervisor
                # owns the env (#1164).
                "is_addon": is_running_in_addon(),
            }
        )

    async def _save_feature_flags(request: Request) -> JSONResponse:
        """Persist UI-edited feature-flag values.

        Routing by per-field origin (see
        :func:`config.get_feature_flag_origin`):

        - **addon**: POST the merged options to Supervisor and return
          ``restart_required=True``. ``start.py`` will re-derive env
          vars from ``config.yaml`` on the next addon boot — but the
          actual restart is fired by the user clicking the global
          Restart Add-on button, NOT by this handler. Web UI edits
          and Configuration-tab edits land in the same place, so the
          two surfaces stay in sync after the restart.
        - **env**: refuse — env var explicitly set wins. Returns
          ``VALIDATION_INVALID_PARAMETER`` with the env var name so
          the UI can surface the locking source.
        - **file** / **default**: merge into the override file in the
          data dir; takes effect on the next
          ``get_global_settings()`` call (cache reset). The response
          still carries ``restart_required=True`` because most
          flag descriptions advertise "Requires restart to take
          effect" — the UI shows the banner regardless of mode.
        """
        from .config import (
            _FEATURE_FLAG_INT_BOUNDS,
            _FEATURE_FLAG_OVERRIDE_FILENAME,
            FEATURE_FLAG_FIELDS,
            get_feature_flag_origin,
        )
        from .utils.data_paths import get_data_dir

        try:
            body = await request.json()
        except (ValueError, TypeError):
            return JSONResponse(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_JSON,
                    "Invalid JSON body",
                ),
                status_code=400,
            )
        if not isinstance(body, dict):
            return JSONResponse(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Request body must be a JSON object",
                ),
                status_code=400,
            )
        raw_flags = body.get("flags", {})
        if not isinstance(raw_flags, dict):
            return JSONResponse(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "'flags' must be an object mapping field names to values",
                ),
                status_code=400,
            )

        # Master beta-gate check (#1164): a sub-flag write is only valid
        # when the master ``enable_beta_features`` is on AFTER the
        # merge. Derive the post-merge master from the payload (if
        # present), otherwise fall back to the live ``Settings`` value.
        # Reject sub-flag writes that try to enable a beta when the
        # resulting master state would still be off — the runtime gate
        # would force them False anyway and the user should know the
        # save was a no-op rather than learning at next startup.
        #
        # Applied in BOTH standalone and addon mode (#1164 follow-up).
        # The earlier "skip in addon mode" carve-out existed because
        # start.py used to auto-write ENABLE_BETA_FEATURES=true from
        # any beta sub-flag presence; now start.py writes the master
        # env from its own options key, so the gate applies uniformly.
        from .config import (
            BETA_FEATURE_FIELDS as _BETA_SUB,
        )

        effective_master = bool(
            raw_flags.get(
                "enable_beta_features",
                getattr(get_global_settings(), "enable_beta_features", False),
            )
        )
        beta_sub_writes = [
            k for k in raw_flags if k in _BETA_SUB and bool(raw_flags[k])
        ]
        if beta_sub_writes and not effective_master:
            return JSONResponse(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    (
                        "Cannot enable beta sub-flag(s) "
                        f"{', '.join(beta_sub_writes)} while the master "
                        "'Enable beta features' toggle is off. Include "
                        "enable_beta_features=true in the same save, or "
                        "flip the master on first."
                    ),
                    context={"rejected": beta_sub_writes},
                ),
                status_code=409,
            )

        # Build the validated override dict. Reject unknown fields and
        # env-locked fields up front so the user gets a precise error
        # instead of a silent no-op. ``addon``-origin fields are now
        # editable — they route through Supervisor below.
        known: dict[str, tuple[str, type]] = {
            fname: (ename, ftype) for fname, ename, ftype in FEATURE_FLAG_FIELDS
        }
        new_overrides: dict[str, Any] = {}
        # Per ``config.get_feature_flag_origin``, addon mode (i.e.
        # ``SUPERVISOR_TOKEN`` set) makes the helper return ``"addon"``
        # for *every* registered flag — there is no path that yields a
        # mixed addon + file/default batch from a single addon-mode UI
        # session. The loop still tracks ``addon_writes`` per-field as
        # belt-and-braces in case a future origin-resolution change
        # breaks that invariant; the post-loop assertion below makes the
        # invariant explicit so a regression fails loudly instead of
        # silently routing a file-mode write through Supervisor.
        addon_writes = False
        file_or_default_writes = False
        for field_name, raw in raw_flags.items():
            if field_name not in known:
                return JSONResponse(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Unknown feature flag: {field_name!r}",
                    ),
                    status_code=400,
                )
            env_name, ftype = known[field_name]
            origin = get_feature_flag_origin(env_name)
            if origin == "addon":
                addon_writes = True
            elif origin in ("file", "default"):
                file_or_default_writes = True
            else:
                return JSONResponse(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        (
                            f"{field_name!r} is locked by {origin} — "
                            f"adjust the {env_name} env var "
                            "(or addon configuration) instead."
                        ),
                    ),
                    status_code=400,
                )
            if ftype is bool:
                if not isinstance(raw, bool):
                    return JSONResponse(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            f"{field_name!r} must be a boolean",
                        ),
                        status_code=400,
                    )
                new_overrides[field_name] = bool(raw)
            elif ftype is int:
                if isinstance(raw, bool) or not isinstance(raw, int):
                    return JSONResponse(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            f"{field_name!r} must be an integer",
                        ),
                        status_code=400,
                    )
                bounds = _FEATURE_FLAG_INT_BOUNDS.get(field_name)
                if bounds is not None and not bounds[0] <= raw <= bounds[1]:
                    return JSONResponse(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            (
                                f"{field_name!r} must be between "
                                f"{bounds[0]} and {bounds[1]}"
                            ),
                        ),
                        status_code=400,
                    )
                new_overrides[field_name] = int(raw)
            else:
                return JSONResponse(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        (f"{field_name!r} has an unsupported type for UI editing"),
                    ),
                    status_code=400,
                )

        # Master-off no longer cascades into sub-flag values (#1164
        # follow-up). The runtime master gate in
        # ``_apply_feature_flag_overrides`` continues to force every
        # beta sub-flag to False whenever the master is off, so the
        # tools stay disabled at runtime regardless of file state.
        # Leaving the sub-flag values in the override file means
        # re-enabling the master restores the user's prior sub-flag
        # selections automatically — without it the user had to
        # re-check each sub-flag individually after every
        # master-off / master-on cycle, which is the wrong UX trade
        # for an opt-in beta surface.
        #
        # The master-gate check above still rejects payloads that try
        # to enable a sub-flag while the effective master is off, so
        # users can't land a "sub=true while master=false in same
        # payload" inconsistency. Pre-existing sub-flag truthy values
        # in the file are kept verbatim.

        # Addon-mode writes go to Supervisor instead of the override file:
        # ``start.py`` reads ``config.yaml`` options on every boot and
        # writes the env vars that Settings consumes, so the override
        # file is ignored anyway (see config.get_feature_flag_origin).
        # POST a merged options dict (current options + this delta) so
        # required schema keys like ``backup_hint`` survive — see
        # _supervisor_merge_and_post_options for the rationale, and the
        # parallel handling in _save_backup_config.
        #
        # Reject mixed-origin batches loudly. The current
        # ``get_feature_flag_origin`` implementation guarantees a single
        # mode per request, but if a future change broke that invariant
        # we would silently route a file/default-origin field through
        # Supervisor (which would reject it as an unknown schema key in
        # production addon mode) — better to fail clearly here.
        if addon_writes and file_or_default_writes:
            return JSONResponse(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    (
                        "Batch contains a mix of addon-origin and "
                        "file/default-origin fields; route each batch "
                        "through a single persistence path."
                    ),
                ),
                status_code=500,
            )
        if addon_writes:
            # ``addon_writes=True`` is equivalent to
            # ``is_running_in_addon()`` (origin=="addon" requires
            # SUPERVISOR_TOKEN per ``get_feature_flag_origin``), so the
            # only guard we still need here is the sidecar-shape
            # ``server is None``. The helpers below catch the missing-
            # token ``RuntimeError`` from ``make_supervisor_httpx_client``
            # as defense-in-depth.
            if server is None:
                return JSONResponse(
                    create_error_response(
                        ErrorCode.INTERNAL_ERROR,
                        "Feature-flag POST requires a live MCP server",
                    ),
                    status_code=500,
                )
            ok, err = await _supervisor_merge_and_post_options(
                server.settings.verify_ssl, new_overrides
            )
            if not ok:
                if err is None:
                    # ``ok=False`` with no error is a contract bug —
                    # bail with INTERNAL_ERROR rather than letting an
                    # AttributeError leak under ``python -O``.
                    return JSONResponse(
                        create_error_response(
                            ErrorCode.INTERNAL_ERROR,
                            "Supervisor helper returned ok=False with no error",
                        ),
                        status_code=500,
                    )
                logger.warning(
                    "Supervisor feature-flag update failed (%s): %s",
                    err.kind,
                    err.message,
                )
                # Transport failures get CONNECTION_FAILED so the UI shows the
                # "is HA reachable" suggestions; supervisor schema rejections
                # get CONFIG_VALIDATION_FAILED with supervisor's real status
                # code preserved so the UI shows the actual 4xx, not a generic
                # 502. See _SupervisorOptionsError for the rationale.
                code = (
                    ErrorCode.CONNECTION_FAILED
                    if err.kind == "transport"
                    else ErrorCode.CONFIG_VALIDATION_FAILED
                )
                return JSONResponse(
                    create_error_response(code, err.message),
                    status_code=err.status_code,
                )
            # Unified restart flow: every save that requires an addon
            # restart (Tools, Server Settings, Backups) returns
            # ``restart_required=True`` and lets the user pick when to
            # fire the actual restart via the global Restart Add-on
            # button. Don't auto-restart from the save handler —
            # supervisor would kill the addon before this JSON response
            # could flush through HA ingress, surfacing as a spurious
            # "Restart failed" alert at the browser.
            return JSONResponse(
                {
                    "success": True,
                    "applied": new_overrides,
                    "mode": "addon",
                    "restart_required": True,
                }
            )

        # Merge with the existing override file so a partial POST
        # only updates the keys it actually included — the front-end
        # POSTs individual changes, not the entire matrix.
        #
        # Three failure modes for the read:
        #
        # * file missing → fresh dict, normal first-write path.
        # * file unreadable (PermissionError, etc.) → 500. We can't
        #   silently fall back to {} and overwrite, because that
        #   would silently drop every flag the user had previously
        #   persisted (the same data we can't read is still on
        #   disk).
        # * file present but corrupt JSON → 409. Same data-loss
        #   hazard: a partial write from a prior crash, blindly
        #   overwriting, erases anything past the corruption point.
        #   Return a clear error so the user can inspect / delete
        #   the file manually.
        path = get_data_dir() / _FEATURE_FLAG_OVERRIDE_FILENAME
        # Serialise concurrent saves on the shared override file so
        # two overlapping requests can't interleave their RMW
        # (#1164 follow-up review — A.2). Lock is held only for the
        # read+merge+atomic-write window; pure validation above does
        # not need it.
        async with _get_override_file_lock():
            existing: dict[str, Any] = {}
            try:
                existing_raw = path.read_text()
            except FileNotFoundError:
                existing_raw = None
            except OSError as exc:
                logger.warning("Cannot read %s", path, exc_info=True)
                return JSONResponse(
                    create_error_response(
                        ErrorCode.INTERNAL_ERROR,
                        (
                            f"Could not read existing feature flags "
                            f"({type(exc).__name__}: {exc}); refusing to "
                            "overwrite to avoid losing prior toggles. "
                            "Check filesystem permissions and retry."
                        ),
                    ),
                    status_code=500,
                )
            if existing_raw is not None:
                try:
                    parsed = json.loads(existing_raw)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "Existing %s is corrupt: %s", path, exc, exc_info=True
                    )
                    return JSONResponse(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            (
                                f"Existing override file at {path} is not "
                                f"valid JSON ({exc}); refusing to overwrite "
                                "to avoid losing prior toggles. Inspect or "
                                "delete the file manually and retry."
                            ),
                        ),
                        status_code=409,
                    )
                if isinstance(parsed, dict):
                    existing = parsed
                # else: non-dict JSON (list, scalar) — treat as empty;
                # we're about to write a dict either way and there's
                # no prior toggle state to preserve from a non-object
                # root.
            existing.update(new_overrides)

            # Atomic write: tmp + rename. ``path.write_text`` is
            # O_TRUNC + write — a crash mid-write leaves an empty /
            # truncated file that the next
            # ``_read_feature_flag_override_file`` call would refuse,
            # losing every prior toggle.
            tmp = path.with_suffix(path.suffix + ".tmp")
            try:
                tmp.write_text(json.dumps(existing, indent=2))
                os.replace(tmp, path)
            except OSError as exc:
                logger.warning("Could not write %s", path, exc_info=True)
                with contextlib.suppress(FileNotFoundError, OSError):
                    tmp.unlink()
                return JSONResponse(
                    create_error_response(
                        ErrorCode.INTERNAL_ERROR,
                        f"Could not persist feature flags: {exc}",
                    ),
                    status_code=500,
                )

        # Publish the change so the same process picks it up on the
        # next ``get_global_settings()`` call. The cached singleton
        # must not return the stale pre-write values to subsequent
        # /api/settings/features GETs.
        #
        # ``restart_required=True`` because the feature flags here gate
        # tool registration, FastMCP transforms, and other startup-time
        # reads. File-mode persists the value, but the live process
        # keeps the old behavior until the MCP host is restarted
        # (Claude Desktop relaunch, Docker container restart, etc.).
        # Surfacing the banner is the same contract Tools, Server
        # Settings, and Backups all advertise.
        _reset_global_settings()
        return JSONResponse(
            {
                "success": True,
                "applied": new_overrides,
                "mode": "file",
                "restart_required": True,
            }
        )

    # ---- Auto-backup routes (#1288) ----

    def _backup_mgr() -> Any:
        settings = get_global_settings()
        client = getattr(server, "client", None) or getattr(server, "_client", None)
        return get_backup_manager(client, settings) if client is not None else None

    def _bad_request(
        message: str,
        *,
        code: ErrorCode = ErrorCode.VALIDATION_INVALID_PARAMETER,
        status: int = 400,
    ) -> JSONResponse:
        return JSONResponse(create_error_response(code, message), status_code=status)

    def _not_found(name: str) -> JSONResponse:
        return JSONResponse(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND, f"Backup {name!r} not found"
            ),
            status_code=404,
        )

    async def _list_backups(request: Request) -> JSONResponse:
        mgr = _backup_mgr()
        if mgr is None:
            return _bad_request("Backup manager unavailable")
        params = request.query_params
        try:
            limit = int(params.get("limit", "500"))
        except ValueError:
            return _bad_request("'limit' must be an integer")
        # Offload sync directory I/O to keep the request handler async-clean.
        entries = await asyncio.to_thread(
            mgr.list_snapshots,
            domain=params.get("domain") or None,
            entity_id=params.get("entity_id") or None,
            limit=max(1, min(10_000, limit)),
        )
        settings = get_global_settings()
        return JSONResponse(
            {
                "success": True,
                "backups": entries,
                "count": len(entries),
                "backup_dir": str(mgr.backup_dir),
                "enabled": mgr.enabled,
                "throttle_minutes": settings.auto_backup_throttle_minutes,
                "retain_per_entity": settings.auto_backup_retain_per_entity,
            }
        )

    async def _view_backup(request: Request) -> JSONResponse:
        mgr = _backup_mgr()
        if mgr is None:
            return _bad_request("Backup manager unavailable")
        name = request.path_params.get("name", "")
        try:
            data = await asyncio.to_thread(mgr.read_snapshot, name)
        except FileNotFoundError:
            return _not_found(name)
        except ValueError as err:
            return _bad_request(str(err))
        return JSONResponse({"success": True, "data": data})

    async def _diff_backup(request: Request) -> JSONResponse:
        import difflib

        import yaml  # type: ignore[import-untyped]

        mgr = _backup_mgr()
        if mgr is None:
            return _bad_request("Backup manager unavailable")
        name = request.path_params.get("name", "")
        try:
            snapshot = await asyncio.to_thread(mgr.read_snapshot, name)
        except FileNotFoundError:
            return _not_found(name)
        except ValueError as err:
            return _bad_request(str(err))
        handler = mgr.handler_for(snapshot["domain"])
        if handler is None:
            return _bad_request(
                f"No handler for domain {snapshot['domain']!r}; cannot diff",
                code=ErrorCode.RESOURCE_NOT_FOUND,
                status=404,
            )
        client = getattr(server, "client", None) or getattr(server, "_client", None)
        # Narrow to transport / HA-API / FS errors so programming bugs
        # propagate to the request handler instead of decorating the diff
        # output with a "_error" sentinel masquerading as entity state.
        from .backup_manager import _CAPTURE_TRANSIENT_ERRORS

        try:
            current = await handler.fetch(client, snapshot["entity_id"])
        except _CAPTURE_TRANSIENT_ERRORS as err:
            current = {"_error": f"{type(err).__name__}: {err}"}
        backup_yaml = yaml.safe_dump(
            snapshot.get("config"), default_flow_style=False, sort_keys=True
        ).splitlines()
        current_yaml = yaml.safe_dump(
            current, default_flow_style=False, sort_keys=True
        ).splitlines()
        diff = list(
            difflib.unified_diff(
                backup_yaml,
                current_yaml,
                fromfile=f"backup:{name}",
                tofile=f"current:{snapshot['entity_id']}",
                lineterm="",
            )
        )
        return JSONResponse(
            {
                "success": True,
                "diff": "\n".join(diff),
                "backup_present": current is not None
                and not (isinstance(current, dict) and "_error" in current),
            }
        )

    async def _restore_backup(request: Request) -> JSONResponse:
        mgr = _backup_mgr()
        if mgr is None:
            return _bad_request("Backup manager unavailable")
        name = request.path_params.get("name", "")
        try:
            result = await mgr.restore_snapshot(name)
        except FileNotFoundError:
            return _not_found(name)
        except (ValueError, LookupError) as err:
            return _bad_request(str(err))
        return JSONResponse({"success": True, "data": result})

    async def _delete_backup(request: Request) -> JSONResponse:
        mgr = _backup_mgr()
        if mgr is None:
            return _bad_request("Backup manager unavailable")
        name = request.path_params.get("name", "")
        try:
            await asyncio.to_thread(mgr.delete_snapshot, name)
        except FileNotFoundError:
            return _not_found(name)
        except ValueError as err:
            return _bad_request(str(err))
        return JSONResponse({"success": True, "deleted": [name]})

    async def _delete_backups_bulk(request: Request) -> JSONResponse:
        mgr = _backup_mgr()
        if mgr is None:
            return _bad_request("Backup manager unavailable")
        params = request.query_params
        older = params.get("older_than_days")
        try:
            older_int = int(older) if older is not None else None
        except ValueError:
            return _bad_request("'older_than_days' must be an integer")
        if (
            params.get("domain") is None
            and params.get("entity_id") is None
            and older_int is None
        ):
            return _bad_request(
                "Bulk delete requires at least one filter "
                "(domain, entity_id, older_than_days)"
            )
        try:
            bulk = await asyncio.to_thread(
                mgr.delete_bulk,
                domain=params.get("domain") or None,
                entity_id=params.get("entity_id") or None,
                older_than_days=older_int,
            )
        except ValueError as err:
            return _bad_request(str(err))
        return JSONResponse(
            {
                "success": True,
                "deleted": bulk["deleted"],
                "failed": bulk["failed"],
                "count": len(bulk["deleted"]),
                "failed_count": len(bulk["failed"]),
            }
        )

    async def _get_advanced_settings(_: Request) -> JSONResponse:
        """Return advanced (non-feature-flag, non-backup) settings + per-field
        origin + editable flag (#1164).

        Mirrors ``_get_feature_flags`` / ``_get_backup_config`` but for
        the ``ADVANCED_SETTINGS_FIELDS`` registry. Most advanced fields
        write to ``feature_flags.json`` via the shared override file in
        either deployment mode. ``ADDON_SYNCED_ADVANCED_FIELDS``
        (currently ``backup_hint``, ``verify_ssl``) are an exception:
        in addon mode they have ``origin="addon"`` (editable) and saves
        route through Supervisor ``/addons/self/options`` so the addon
        Configuration tab and the web UI share state (#1164 follow-up).
        """
        from .config import (
            _ADVANCED_SETTINGS_BOUNDS,
            _ADVANCED_SETTINGS_CHOICES,
            ADVANCED_SETTINGS_FIELDS,
            OAUTH_MODE_TOKEN,
            _read_feature_flag_override_file,
            get_global_settings,
        )

        settings = get_global_settings()
        # Read the override file ONCE for this GET — origin lookup is
        # called per field, and re-reading the file 17+ times would
        # produce duplicate WARNINGs on a corrupt file (one per field).
        overrides = _read_feature_flag_override_file()
        fields: list[dict[str, Any]] = []
        for (
            fname,
            env_name,
            ftype,
            section,
            registry_editable,
        ) in ADVANCED_SETTINGS_FIELDS:
            origin = _origin_for_advanced_field(env_name, overrides=overrides)
            value: Any = getattr(settings, fname, None)
            # Mask the token: never echo the actual long-lived access
            # token to the UI. The OAuth-mode sentinel survives so
            # operators can tell connection mode at a glance.
            if fname == "homeassistant_token":
                value = "*****" if value and value != OAUTH_MODE_TOKEN else value
            row: dict[str, Any] = {
                "field": fname,
                "env_var": env_name,
                "value": value,
                "type": ftype.__name__,
                "section": section,
                "origin": origin,
                # Env-pin makes the field read-only regardless of the
                # registry's ``editable`` flag. Display-only rows from
                # the registry (homeassistant_url / _token) stay locked
                # forever.
                "editable": registry_editable and origin != "env",
            }
            bounds = _ADVANCED_SETTINGS_BOUNDS.get(fname)
            if bounds is not None:
                row["min"], row["max"] = bounds
            choices = _ADVANCED_SETTINGS_CHOICES.get(fname)
            if choices is not None:
                row["choices"] = list(choices)
            fields.append(row)
        return JSONResponse({"fields": fields, "is_addon": is_running_in_addon()})

    def _origin_for_advanced_field(
        env_name: str, overrides: dict[str, Any] | None = None
    ) -> str:
        """Origin for an ADVANCED_SETTINGS_FIELDS entry.

        Returns ``'addon' | 'env' | 'file' | 'default'``.

        ``'addon'`` is returned in addon mode for fields that live in
        both the registry and the addon's config.yaml schema (the
        ``ADDON_SYNCED_ADVANCED_FIELDS`` set). For those, writes route
        through Supervisor instead of the override file so the addon
        Configuration tab and this web UI share state (#1164
        follow-up). Other env-pinned fields stay ``'env'`` (locked).

        Callers iterating ADVANCED_SETTINGS_FIELDS should pass a
        pre-read ``overrides`` dict so the override file isn't re-read
        N times per page render.
        """
        from .config import (
            ADDON_SYNCED_ADVANCED_FIELDS,
            ADVANCED_SETTINGS_FIELDS,
            _read_feature_flag_override_file,
        )

        fname = next(
            (f for f, e, *_ in ADVANCED_SETTINGS_FIELDS if e == env_name), None
        )
        if (
            is_running_in_addon()
            and fname is not None
            and fname in ADDON_SYNCED_ADVANCED_FIELDS
        ):
            return "addon"
        if os.environ.get(env_name) is not None:
            return "env"
        if overrides is None:
            overrides = _read_feature_flag_override_file()
        if fname is not None and fname in overrides:
            return "file"
        return "default"

    async def _save_advanced_settings(request: Request) -> JSONResponse:
        """Persist UI-edited advanced settings (#1164).

        Two persistence sinks depending on field origin:

        - ``ADDON_SYNCED_ADVANCED_FIELDS`` (``backup_hint``,
          ``verify_ssl``) in addon mode → write goes through
          Supervisor ``/addons/self/options`` so the addon
          Configuration tab and this web UI share state.
        - Everything else → atomic write to ``feature_flags.json``
          (the shared override file used by feature flags), then
          ``_reset_global_settings()`` so the next read picks up the
          new value.

        Validation chain per field:
        - Unknown field → 400 ``VALIDATION_INVALID_PARAMETER``.
        - ``editable=False`` registry entry → 409 (display-only).
        - Env-pinned (``origin=='env'`` and NOT addon-synced) → 409
          with env var name.
        - Type mismatch → 400.
        - Bounds violation → 400.
        - Choices violation → 400.

        Either sink responds with ``restart_required=True`` so the UI
        shows the banner — most advanced fields gate one-time startup
        paths (REST client construction, logging setup, MCP handshake
        metadata, tool-module filtering, etc.).
        """
        from .config import (
            _ADVANCED_SETTINGS_BOUNDS,
            _ADVANCED_SETTINGS_CHOICES,
            _FEATURE_FLAG_OVERRIDE_FILENAME,
            ADVANCED_SETTINGS_FIELDS,
        )
        from .utils.data_paths import get_data_dir

        try:
            body = await request.json()
        except (ValueError, TypeError):
            return JSONResponse(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_JSON,
                    "Invalid JSON body",
                ),
                status_code=400,
            )
        if not isinstance(body, dict):
            return JSONResponse(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Request body must be a JSON object",
                ),
                status_code=400,
            )

        from .config import ADDON_SYNCED_ADVANCED_FIELDS

        registry = {f: (e, t, s, ed) for f, e, t, s, ed in ADVANCED_SETTINGS_FIELDS}
        new_overrides: dict[str, Any] = {}
        addon_writes_present = False
        addon_mode = is_running_in_addon()
        for fname, raw in body.items():
            if fname not in registry:
                return JSONResponse(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Unknown advanced field: {fname!r}",
                    ),
                    status_code=400,
                )
            env_name, ftype, _section, registry_editable = registry[fname]
            if not registry_editable:
                return JSONResponse(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"{fname!r} is display-only — modify via env var "
                        "or addon configuration.",
                    ),
                    status_code=409,
                )
            # Addon-synced fields (e.g. backup_hint, verify_ssl) are
            # editable in addon mode even though their env vars are
            # set — start.py rewrites them from /data/options.json on
            # every boot, so we route the user's write through
            # Supervisor instead of the override file (#1164 follow-up).
            is_addon_synced = addon_mode and fname in ADDON_SYNCED_ADVANCED_FIELDS
            if is_addon_synced:
                addon_writes_present = True
            elif os.environ.get(env_name) is not None:
                return JSONResponse(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"{fname!r} is set via {env_name} env var — "
                        "unset it to edit here.",
                        context={"env_var": env_name},
                    ),
                    status_code=409,
                )

            coerced: Any
            if ftype is bool:
                if not isinstance(raw, bool):
                    return _bad_advanced_type(fname, ftype, raw)
                coerced = raw
            elif ftype is int:
                if isinstance(raw, bool) or not isinstance(raw, int):
                    return _bad_advanced_type(fname, ftype, raw)
                coerced = int(raw)
            elif ftype is float:
                if isinstance(raw, bool) or not isinstance(raw, int | float):
                    return _bad_advanced_type(fname, ftype, raw)
                coerced = float(raw)
            elif ftype is str:
                if not isinstance(raw, str):
                    return _bad_advanced_type(fname, ftype, raw)
                if "\x00" in raw:
                    return JSONResponse(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            f"{fname!r} contains a null byte; rejected.",
                        ),
                        status_code=400,
                    )
                coerced = raw
            else:
                return _bad_advanced_type(fname, ftype, raw)

            bounds = _ADVANCED_SETTINGS_BOUNDS.get(fname)
            if bounds is not None and not (bounds[0] <= coerced <= bounds[1]):
                return JSONResponse(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"{fname!r} must be between {bounds[0]} and "
                        f"{bounds[1]} (got {coerced}).",
                    ),
                    status_code=400,
                )
            choices = _ADVANCED_SETTINGS_CHOICES.get(fname)
            if choices is not None and coerced not in choices:
                return JSONResponse(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"{fname!r} must be one of {list(choices)} (got {coerced!r}).",
                    ),
                    status_code=400,
                )
            new_overrides[fname] = coerced

        if not new_overrides:
            return JSONResponse(
                {
                    "success": True,
                    "applied": {},
                    "mode": "file",
                    "restart_required": False,
                }
            )

        # Addon-route: at least one field is an addon-synced advanced
        # field (backup_hint / verify_ssl in addon mode). Batch every
        # write in this call into a single Supervisor options POST
        # — same merge-then-replace pattern as the feature-flag
        # addon-route, including the "single persistence path"
        # invariant: we don't mix override-file writes and Supervisor
        # writes from the same call. If a future caller submits both
        # addon-synced and non-addon fields in one batch (none today
        # are non-addon AND non-locked in addon mode), reject loudly
        # instead of routing them through different sinks (#1164
        # follow-up).
        if addon_writes_present:
            if not addon_mode:
                # Defensive: addon_writes_present should imply addon_mode
                # because is_addon_synced is gated on it above.
                return JSONResponse(
                    create_error_response(
                        ErrorCode.INTERNAL_ERROR,
                        "Inconsistent addon-route classification",
                    ),
                    status_code=500,
                )
            file_only = {
                k: v
                for k, v in new_overrides.items()
                if k not in ADDON_SYNCED_ADVANCED_FIELDS
            }
            if file_only:
                return JSONResponse(
                    create_error_response(
                        ErrorCode.INTERNAL_ERROR,
                        (
                            "Mixed addon-synced and override-file advanced "
                            "writes in one batch; the UI should split these "
                            f"into separate POSTs ({sorted(file_only)})."
                        ),
                    ),
                    status_code=500,
                )
            if server is None:
                return JSONResponse(
                    create_error_response(
                        ErrorCode.INTERNAL_ERROR,
                        "Advanced settings POST requires a live MCP server",
                    ),
                    status_code=500,
                )
            ok, sup_err = await _supervisor_merge_and_post_options(
                server.settings.verify_ssl, new_overrides
            )
            if not ok:
                if sup_err is None:
                    # ``ok=False`` with no error is a contract bug in
                    # the helper, not a user-actionable failure. Bail
                    # with INTERNAL_ERROR instead of letting an
                    # AttributeError leak under ``python -O`` (where
                    # the previous ``assert`` was stripped).
                    return JSONResponse(
                        create_error_response(
                            ErrorCode.INTERNAL_ERROR,
                            "Supervisor helper returned ok=False with no error",
                        ),
                        status_code=500,
                    )
                # Mirror the sibling ``_save_feature_flags`` /
                # ``_save_backup_config`` handlers: log loudly before
                # returning so addon-log forensics survive the user
                # closing the tab.
                logger.warning(
                    "Supervisor advanced-settings update failed (%s): %s",
                    sup_err.kind,
                    sup_err.message,
                )
                code = (
                    ErrorCode.CONNECTION_FAILED
                    if sup_err.kind == "transport"
                    else ErrorCode.CONFIG_VALIDATION_FAILED
                )
                return JSONResponse(
                    create_error_response(code, sup_err.message),
                    status_code=sup_err.status_code,
                )
            return JSONResponse(
                {
                    "success": True,
                    "applied": new_overrides,
                    "mode": "addon",
                    "restart_required": True,
                }
            )

        # Merge into the existing override file via atomic write (same
        # path used by _save_feature_flags so a single file holds both
        # advanced + feature-flag overrides). Lock-serialised against
        # concurrent feature-flag saves on the same file (#1164
        # follow-up review — A.2).
        path = get_data_dir() / _FEATURE_FLAG_OVERRIDE_FILENAME
        async with _get_override_file_lock():
            existing: dict[str, Any] = {}
            try:
                existing_raw = path.read_text()
            except FileNotFoundError:
                existing_raw = None
            except OSError as exc:
                logger.warning("Cannot read %s", path, exc_info=True)
                return JSONResponse(
                    create_error_response(
                        ErrorCode.INTERNAL_ERROR,
                        f"Could not read existing override file "
                        f"({type(exc).__name__}: {exc}); refusing to "
                        "overwrite to avoid losing prior toggles.",
                    ),
                    status_code=500,
                )
            if existing_raw is not None:
                try:
                    parsed = json.loads(existing_raw)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "Existing %s is corrupt: %s", path, exc, exc_info=True
                    )
                    return JSONResponse(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            f"Existing override file at {path} is not "
                            f"valid JSON ({exc}); refusing to overwrite. "
                            "Inspect or delete the file manually and retry.",
                        ),
                        status_code=409,
                    )
                if isinstance(parsed, dict):
                    existing = parsed
            existing.update(new_overrides)

            try:
                _atomic_write_json(path, existing)
            except OSError as exc:
                logger.warning("Could not write %s", path, exc_info=True)
                return JSONResponse(
                    create_error_response(
                        ErrorCode.INTERNAL_ERROR,
                        f"Could not persist advanced settings: {exc}",
                    ),
                    status_code=500,
                )

        _reset_global_settings()
        return JSONResponse(
            {
                "success": True,
                "applied": new_overrides,
                "mode": "file",
                "restart_required": True,
            }
        )

    def _bad_advanced_type(fname: str, ftype: type, raw: Any) -> JSONResponse:
        return JSONResponse(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"{fname!r} expects {ftype.__name__}, got {type(raw).__name__}.",
            ),
            status_code=400,
        )

    async def _get_backup_config(_: Request) -> JSONResponse:
        """Return live auto-backup config + per-field origin + editable flag.

        Per-field origin/editable matrix (see ``config.get_backup_setting_origin``):
        - ``addon``: editable — POST routes through Supervisor.
        - ``env``: read-only — env var wins; user must unset to edit.
        - ``file``/``default``: editable — POST writes the override file.
        """
        settings = get_global_settings()
        addon_mode = is_running_in_addon()
        fields = []
        for field_name, env_name, _ftype in BACKUP_OVERRIDE_FIELDS:
            origin = get_backup_setting_origin(env_name)
            editable = origin in ("addon", "file", "default")
            fields.append(
                {
                    "field": field_name,
                    "env_var": env_name,
                    "value": getattr(settings, field_name),
                    "origin": origin,
                    "editable": editable,
                }
            )
        return JSONResponse(
            {
                "success": True,
                "is_addon": addon_mode,
                "fields": fields,
            }
        )

    def _validate_backup_payload(payload: Any) -> tuple[dict[str, Any], str | None]:
        """Coerce and bounds-check the POST body. Returns (clean, error_msg)."""
        if not isinstance(payload, dict):
            return {}, "Body must be a JSON object"
        clean: dict[str, Any] = {}
        for field_name, _env_name, ftype in BACKUP_OVERRIDE_FIELDS:
            if field_name not in payload:
                continue
            raw = payload[field_name]
            if ftype is bool:
                if isinstance(raw, bool):
                    value: Any = raw
                elif isinstance(raw, int):
                    value = bool(raw)
                elif isinstance(raw, str):
                    s = raw.strip().lower()
                    if s in ("true", "1", "yes", "on"):
                        value = True
                    elif s in ("false", "0", "no", "off"):
                        value = False
                    else:
                        return {}, f"Invalid boolean for {field_name}: {raw!r}"
                else:
                    return {}, f"Invalid value for {field_name}: {raw!r}"
            elif ftype is int:
                if isinstance(raw, bool) or not isinstance(raw, int | str):
                    return {}, f"Invalid integer for {field_name}: {raw!r}"
                try:
                    value = int(raw)
                except (ValueError, TypeError):
                    return {}, f"Invalid integer for {field_name}: {raw!r}"
                if field_name == "auto_backup_throttle_minutes" and not (
                    0 <= value <= 1440
                ):
                    return {}, "auto_backup_throttle_minutes must be 0..1440"
                if field_name == "auto_backup_retain_per_entity" and not (
                    1 <= value <= 10_000
                ):
                    return {}, "auto_backup_retain_per_entity must be 1..10000"
                if field_name == "auto_backup_calendar_lookahead_days" and not (
                    1 <= value <= 365
                ):
                    return (
                        {},
                        "auto_backup_calendar_lookahead_days must be 1..365",
                    )
            elif ftype is str:
                if not isinstance(raw, str):
                    return {}, f"Invalid string for {field_name}: {raw!r}"
                if "\x00" in raw:
                    return {}, f"{field_name} must not contain null bytes"
                value = raw
            else:
                continue
            clean[field_name] = value
        if not clean:
            return {}, "No editable auto-backup fields in body"
        return clean, None

    async def _save_backup_config(request: Request) -> JSONResponse:
        """Persist auto-backup config edits and publish to the live process.

        Routing:
        - Addon mode: POST ``/addons/self/options`` (with the existing
          options merged so required schema keys like ``backup_hint``
          survive the full-replacement validation) and return
          ``restart_required=True``. ``start.py`` will re-derive env
          vars from ``config.yaml`` on the next addon boot, but the
          actual restart is fired by the user clicking the global
          Restart Add-on button — NOT by this handler. Same unified
          flow as the Tools and Server Settings save endpoints.
        - Standalone (file) mode: refuse any field that's pinned by an
          env var (process or ``.env``) — return 409 with the offending
          names so the UI can refresh and show the read-only banner.
          Editable fields merge into
          ``<data_dir>/backup_settings.json`` and a Settings cache
          reset publishes them immediately, hence
          ``restart_required=False``.
        """
        try:
            payload = await request.json()
        except (ValueError, json.JSONDecodeError):
            return _bad_request("Invalid JSON body")
        clean, err = _validate_backup_payload(payload)
        if err is not None:
            return _bad_request(err)

        if is_running_in_addon():
            # ``is_running_in_addon()`` checks SUPERVISOR_TOKEN — the
            # helpers below also catch the missing-token ``RuntimeError``
            # from ``make_supervisor_httpx_client`` as defense-in-depth,
            # so we only still need the sidecar-shape ``server is None``
            # guard.
            if server is None:
                # Addon mode without a live server means we're in the
                # stdio sidecar — but addon detection should already be
                # False there. Defensive guard for type-checker + future
                # refactors.
                return _bad_request(
                    "Backup settings POST requires a live MCP server",
                    code=ErrorCode.INTERNAL_ERROR,
                    status=500,
                )
            # Merge ``clean`` into the *full* current options before posting.
            # Supervisor validates against the addon schema and rejects any
            # body missing a required key (notably ``backup_hint`` on the
            # production / dev manifests). A previous version of this code
            # shipped just the auto-backup fields, producing
            # ``addon_configuration_invalid_error: Missing option
            # 'backup_hint' in root`` from supervisor and a confusing 400 in
            # the UI even though the user only changed unrelated fields.
            ok, sup_err = await _supervisor_merge_and_post_options(
                server.settings.verify_ssl, clean
            )
            if not ok:
                assert sup_err is not None
                logger.warning(
                    "Supervisor backup-config update failed (%s): %s",
                    sup_err.kind,
                    sup_err.message,
                )
                code = (
                    ErrorCode.CONNECTION_FAILED
                    if sup_err.kind == "transport"
                    else ErrorCode.CONFIG_VALIDATION_FAILED
                )
                return JSONResponse(
                    create_error_response(code, sup_err.message),
                    status_code=sup_err.status_code,
                )
            # Unified restart flow — see _save_feature_flags for the
            # rationale. Don't auto-restart from a save handler; the
            # global Restart Add-on button is the single restart path.
            return JSONResponse(
                {
                    "success": True,
                    "applied": clean,
                    "mode": "addon",
                    "restart_required": True,
                }
            )

        # Standalone (file) mode — refuse to override env-pinned fields.
        rejected: list[dict[str, str]] = []
        for field_name in list(clean.keys()):
            env_name = next(
                en for fn, en, _ in BACKUP_OVERRIDE_FIELDS if fn == field_name
            )
            if os.environ.get(env_name) is not None:
                rejected.append({"field": field_name, "env_var": env_name})
                del clean[field_name]
        if rejected:
            return JSONResponse(
                {
                    "success": False,
                    "error": {
                        "code": "env_var_pinned",
                        "message": (
                            "Some fields are set via environment variable and "
                            "cannot be changed from the web UI. Unset the "
                            "env var(s) and reload."
                        ),
                        "rejected": rejected,
                    },
                },
                status_code=409,
            )
        if not clean:
            return _bad_request("No editable auto-backup fields after env-var filter")
        current = _load_backup_settings_override()
        current.update(clean)
        if not _save_backup_settings_override(current):
            return JSONResponse(
                create_error_response(
                    ErrorCode.CONFIG_VALIDATION_FAILED,
                    "Failed to persist override file",
                ),
                status_code=500,
            )
        # Drop the cached Settings so the next read sees the merged value.
        # File-mode auto-backup settings take effect immediately on the
        # next ``get_global_settings()`` read — no restart needed, hence
        # ``restart_required=False``. The JS uses the same field name on
        # every save endpoint to decide whether to surface the
        # restart-required banner.
        _reset_global_settings()
        return JSONResponse(
            {
                "success": True,
                "applied": clean,
                "mode": "file",
                "restart_required": False,
            }
        )

    handlers: dict[str, Any] = {
        "root_page": _root_page,
        "settings_page": _settings_page,
        "get_tools": _get_tools,
        "save_tools": _save_tools,
        "restart_addon": _restart_addon,
        "settings_info": _settings_info,
        "get_feature_flags": _get_feature_flags,
        "save_feature_flags": _save_feature_flags,
        "get_advanced_settings": _get_advanced_settings,
        "save_advanced_settings": _save_advanced_settings,
        "list_backups": _list_backups,
        "view_backup": _view_backup,
        "diff_backup": _diff_backup,
        "restore_backup": _restore_backup,
        "delete_backup": _delete_backup,
        "delete_backups_bulk": _delete_backups_bulk,
        "get_backup_config": _get_backup_config,
        "save_backup_config": _save_backup_config,
    }

    # Tool security policies. The main server attaches an
    # ApprovalQueue to the server object once PolicyMiddleware is wired
    # in. Only the main server can serve the live pending/approve/deny
    # endpoints because the queue is in-memory; the sidecar (or a main
    # server without the queue attribute yet) falls back to stub handlers
    # that serve config GET/PUT and return 503 for live approval routes.
    approval_queue = (
        getattr(server, "approval_queue", None) if server is not None else None
    )
    if not is_sidecar and approval_queue is not None:
        from .policy.handlers import build_policy_handlers

        handlers.update(
            build_policy_handlers(
                data_dir=get_data_dir(),
                queue=approval_queue,
                server=server,
            )
        )
    else:
        handlers.update(_build_stub_policy_handlers(data_dir=get_data_dir()))

    return handlers


def register_settings_routes(
    mcp: FastMCP,
    server: HomeAssistantSmartMCPServer,
    secret_path: str = "",
) -> None:
    """Register the settings UI HTTP routes on the FastMCP Starlette app.

    The routes are mounted under ``secret_path`` so HTTP clients (Docker
    / standalone) need the same secret to reach the UI as they do to
    reach the MCP endpoint itself — there's no native auth on FastMCP
    custom routes (they bypass ``RequireAuthMiddleware``), so this
    matches the auth-by-obscurity model the rest of the server uses for
    those modes. In add-on mode (``SUPERVISOR_TOKEN`` set) the routes
    are *also* mounted at root so HA ingress can proxy to ``localhost:9583/``
    and serve the "Open Web UI" button. Stdio transports use a separate
    side-process sidecar instead — see :mod:`ha_mcp.stdio_settings_sidecar`.

    Args:
        mcp: The FastMCP instance to register routes on.
        server: The HomeAssistantSmartMCPServer wrapping ``mcp``.
        secret_path: The MCP secret path (e.g. ``/private_xxx`` or
            ``/mcp``). Required for non-add-on HTTP modes; if empty in
            non-add-on mode, the function logs a warning and registers
            nothing rather than expose the routes publicly.
    """
    handlers = build_settings_handlers(server)
    secret_prefix = secret_path.rstrip("/") if secret_path else ""
    is_addon = is_running_in_addon()

    if not is_addon and not secret_prefix:
        logger.warning(
            "register_settings_routes: not in add-on mode and no secret_path "
            "provided — settings UI HTTP routes not registered (would otherwise "
            "be publicly reachable). Pass MCP_SECRET_PATH or run as add-on."
        )
        return

    if is_addon:
        # Root mount lets HA ingress proxy localhost:9583/ → settings UI.
        # Direct port 9583 LAN access also reaches these routes; in this
        # respect they share the existing add-on networking model where
        # port 9583 is exposed via host_network and the secret path is
        # the auth for direct access. Document this in DOCS.md.
        mcp.custom_route("/", methods=["GET"])(handlers["root_page"])
        mcp.custom_route("/settings", methods=["GET"])(handlers["settings_page"])
        mcp.custom_route("/api/settings/tools", methods=["GET"])(handlers["get_tools"])
        mcp.custom_route("/api/settings/tools", methods=["POST"])(
            handlers["save_tools"]
        )
        mcp.custom_route("/api/settings/restart", methods=["POST"])(
            handlers["restart_addon"]
        )
        mcp.custom_route("/api/settings/info", methods=["GET"])(
            handlers["settings_info"]
        )
        mcp.custom_route("/api/settings/features", methods=["GET"])(
            handlers["get_feature_flags"]
        )
        mcp.custom_route("/api/settings/features", methods=["POST"])(
            handlers["save_feature_flags"]
        )
        # Advanced settings endpoints (#1164)
        mcp.custom_route("/api/settings/advanced", methods=["GET"])(
            handlers["get_advanced_settings"]
        )
        mcp.custom_route("/api/settings/advanced", methods=["POST"])(
            handlers["save_advanced_settings"]
        )
        # Auto-backup endpoints (#1288)
        mcp.custom_route("/api/settings/backups", methods=["GET"])(
            handlers["list_backups"]
        )
        mcp.custom_route("/api/settings/backups", methods=["DELETE"])(
            handlers["delete_backups_bulk"]
        )
        mcp.custom_route("/api/settings/backups/{name}", methods=["GET"])(
            handlers["view_backup"]
        )
        mcp.custom_route("/api/settings/backups/{name}/diff", methods=["GET"])(
            handlers["diff_backup"]
        )
        mcp.custom_route("/api/settings/backups/{name}/restore", methods=["POST"])(
            handlers["restore_backup"]
        )
        mcp.custom_route("/api/settings/backups/{name}", methods=["DELETE"])(
            handlers["delete_backup"]
        )
        mcp.custom_route("/api/settings/backup-config", methods=["GET"])(
            handlers["get_backup_config"]
        )
        mcp.custom_route("/api/settings/backup-config", methods=["POST"])(
            handlers["save_backup_config"]
        )
        # Tool security policies endpoints
        mcp.custom_route("/api/policy/config", methods=["GET"])(
            handlers["policy_get_config"]
        )
        mcp.custom_route("/api/policy/config", methods=["PUT"])(
            handlers["policy_put_config"]
        )
        mcp.custom_route("/api/policy/pending", methods=["GET"])(
            handlers["policy_get_pending"]
        )
        mcp.custom_route("/api/policy/approve", methods=["POST"])(
            handlers["policy_post_approve"]
        )
        mcp.custom_route("/api/policy/deny", methods=["POST"])(
            handlers["policy_post_deny"]
        )
        mcp.custom_route("/api/policy/tool-schema", methods=["GET"])(
            handlers["policy_get_tool_schema"]
        )
        mcp.custom_route("/api/policy/value-source", methods=["GET"])(
            handlers["policy_get_value_source"]
        )

    if secret_prefix:
        # Mount under the MCP secret path so Docker / standalone clients
        # need the same secret to reach the UI as they do for the MCP
        # endpoint. The frontend uses relative fetches (./api/settings/...)
        # so the JS works at either prefix unchanged.
        mcp.custom_route(f"{secret_prefix}/settings", methods=["GET"])(
            handlers["settings_page"]
        )
        mcp.custom_route(f"{secret_prefix}/api/settings/tools", methods=["GET"])(
            handlers["get_tools"]
        )
        mcp.custom_route(f"{secret_prefix}/api/settings/tools", methods=["POST"])(
            handlers["save_tools"]
        )
        mcp.custom_route(f"{secret_prefix}/api/settings/restart", methods=["POST"])(
            handlers["restart_addon"]
        )
        mcp.custom_route(f"{secret_prefix}/api/settings/info", methods=["GET"])(
            handlers["settings_info"]
        )
        mcp.custom_route(f"{secret_prefix}/api/settings/features", methods=["GET"])(
            handlers["get_feature_flags"]
        )
        mcp.custom_route(f"{secret_prefix}/api/settings/features", methods=["POST"])(
            handlers["save_feature_flags"]
        )
        # Advanced settings endpoints (#1164)
        mcp.custom_route(f"{secret_prefix}/api/settings/advanced", methods=["GET"])(
            handlers["get_advanced_settings"]
        )
        mcp.custom_route(f"{secret_prefix}/api/settings/advanced", methods=["POST"])(
            handlers["save_advanced_settings"]
        )
        # Auto-backup endpoints (#1288)
        mcp.custom_route(f"{secret_prefix}/api/settings/backups", methods=["GET"])(
            handlers["list_backups"]
        )
        mcp.custom_route(f"{secret_prefix}/api/settings/backups", methods=["DELETE"])(
            handlers["delete_backups_bulk"]
        )
        mcp.custom_route(
            f"{secret_prefix}/api/settings/backups/{{name}}", methods=["GET"]
        )(handlers["view_backup"])
        mcp.custom_route(
            f"{secret_prefix}/api/settings/backups/{{name}}/diff", methods=["GET"]
        )(handlers["diff_backup"])
        mcp.custom_route(
            f"{secret_prefix}/api/settings/backups/{{name}}/restore", methods=["POST"]
        )(handlers["restore_backup"])
        mcp.custom_route(
            f"{secret_prefix}/api/settings/backups/{{name}}", methods=["DELETE"]
        )(handlers["delete_backup"])
        mcp.custom_route(
            f"{secret_prefix}/api/settings/backup-config", methods=["GET"]
        )(handlers["get_backup_config"])
        mcp.custom_route(
            f"{secret_prefix}/api/settings/backup-config", methods=["POST"]
        )(handlers["save_backup_config"])
        # Tool security policies endpoints
        mcp.custom_route(f"{secret_prefix}/api/policy/config", methods=["GET"])(
            handlers["policy_get_config"]
        )
        mcp.custom_route(f"{secret_prefix}/api/policy/config", methods=["PUT"])(
            handlers["policy_put_config"]
        )
        mcp.custom_route(f"{secret_prefix}/api/policy/pending", methods=["GET"])(
            handlers["policy_get_pending"]
        )
        mcp.custom_route(f"{secret_prefix}/api/policy/approve", methods=["POST"])(
            handlers["policy_post_approve"]
        )
        mcp.custom_route(f"{secret_prefix}/api/policy/deny", methods=["POST"])(
            handlers["policy_post_deny"]
        )
        mcp.custom_route(f"{secret_prefix}/api/policy/tool-schema", methods=["GET"])(
            handlers["policy_get_tool_schema"]
        )
        mcp.custom_route(f"{secret_prefix}/api/policy/value-source", methods=["GET"])(
            handlers["policy_get_value_source"]
        )
