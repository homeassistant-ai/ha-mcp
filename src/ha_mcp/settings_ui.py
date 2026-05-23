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
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, NotRequired, TypedDict

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
    # net before config edits and as the recovery path after them. Added
    # per #966 alongside the per-tool approval middleware so even users
    # who aggressively disable everything keep a working backup tool.
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
# source file (tools_yaml_config.py, tools_filesystem.py, tools_mcp_component.py)
# — a future rename or removal needs to land in both places.
FEATURE_GATED_TOOLS: dict[str, ToolStub] = {
    "ha_config_set_yaml": {
        "title": "Set YAML Config",
        "primary_tag": "System",
        "description": "Add, replace, or remove top-level keys in configuration.yaml or package files.",
        "disabled_by": "enable_yaml_config_editing",
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


class ToolVisibilityResult(NamedTuple):
    """Outcome of applying ``tool_config.json`` to the FastMCP instance.

    Attributes:
        pinned_names: Tools the user explicitly pinned via the UI. The
            server adds these to ``always_visible`` on top of the
            DEFAULT_PINNED_TOOLS set so they bypass the search transform.
        enabled_names: Tools whose state is explicitly ``"enabled"`` in
            ``tool_config.json``. The server subtracts these from
            ``DEFAULT_PINNED_TOOLS`` when building the effective pinned
            set so users can unpin a default-pinned tool by toggling it
            to plain "enabled" in the Tools tab. Tools with no entry in
            the config (the common case) are NOT in this set, so they
            keep their default pinning.
    """

    pinned_names: set[str]
    enabled_names: set[str]


def apply_tool_visibility(
    mcp: FastMCP,
    config: dict[str, Any],
    settings: Settings,
) -> ToolVisibilityResult:
    """Apply tool visibility from config, respecting safety toggles.

    Args:
        mcp: The FastMCP instance to enable/disable tools on.
        config: The tool_config.json contents (per-tool states).
        settings: The server Settings (for enable_yaml_config_editing etc.).

    Returns:
        A :class:`ToolVisibilityResult` carrying the user-pinned tools
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

    return ToolVisibilityResult(
        pinned_names=pinned_names, enabled_names=enabled_names
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
  <button class="tab" data-panel="policies">Policies</button>
</div>
<div class="panel active" id="panel-tools">
  <div class="readonly-notice">
    Server-wide features (Tool Search, YAML config editing, filesystem
    tools, etc.) appear in both the <strong>Server Settings</strong>
    tab and the add-on Configuration page — they're the same settings
    either way. Add-on users edit them on the Configuration page;
    every other install (Claude Desktop, Docker, standalone) edits
    them in the Server Settings tab. Changes require an MCP-host
    restart to apply.
  </div>
  <div class="pin-notice show" id="pinNotice">
    Pin toggles only take effect when Tool Search is enabled — either
    in the Server Settings tab or, for add-on users, the add-on
    Configuration page (same setting either way). Without Tool Search,
    all enabled tools are always visible and pinning has no extra
    effect.
  </div>
  <div class="restart-notice" id="restartNotice">
    <span class="restart-notice-text" id="restartNoticeText">
      ⚠ Changes saved. Restart ha-mcp for them to take effect — disabled
      tools will be fully removed from the MCP tool list on next startup.
    </span>
    <button class="restart-btn" id="restartBtn" style="display:none">Restart Add-on</button>
  </div>
  <div class="summary" id="summary"></div>
  <input type="text" class="search" id="search" placeholder="Search tools...">
  <div id="groups"></div>
</div>
<div class="panel" id="panel-server">
  <div class="features-sub">
    Tool Search, beta-flagged features. Changes require an MCP-host restart
    to take effect (close + reopen Claude Desktop, restart the add-on, etc.).
  </div>
  <div id="featuresBody"></div>
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
<div class="panel" id="panel-policies">
  <h2>Per-tool approval policies</h2>
  <p class="features-sub">
    Opt-in gating for high-stakes tool calls (issue #966). When enabled, the
    AI must obtain your approval here before executing tools that match a
    rule. See the DOCS for predicate operators
    (eq, neq, in, not_in, regex, contains, exists, gt, lt).
  </p>
  <section id="policy-settings" style="margin-bottom:16px">
    <div class="feature-row">
      <div class="feature-info">
        <div class="feature-name">Enabled</div>
        <div class="feature-help">Master switch — when off, every tool runs without policy checks.</div>
      </div>
      <div class="feature-control">
        <label class="switch">
          <input type="checkbox" id="policy-enabled">
          <span class="slider"></span>
        </label>
      </div>
    </div>
    <div class="feature-row">
      <div class="feature-info">
        <div class="feature-name">Default action</div>
        <div class="feature-help">
          <strong>Allow</strong> — only matched rules gate.
          <strong>Require approval</strong> — every tool needs approval unless
          a rule matches and overrides.
        </div>
      </div>
      <div class="feature-control">
        <select id="policy-default-action">
          <option value="allow">Allow</option>
          <option value="require_approval">Require approval</option>
        </select>
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
  </section>

  <section id="policy-pending" style="margin-bottom:16px">
    <h3 style="font-size:1rem;margin-bottom:8px">Pending approvals</h3>
    <div id="policy-pending-list" class="backup-empty">No pending approvals.</div>
  </section>

  <section id="policy-rules">
    <h3 style="font-size:1rem;margin-bottom:8px">Rules (JSON)</h3>
    <p class="features-sub">
      Edit the <code>rules</code> array as JSON. Each rule has
      <code>tool_name</code>, optional <code>when</code> predicates, and
      optional <code>remember_minutes</code>.
    </p>
    <textarea id="policy-rules-json" rows="14"
              style="width:100%; font-family:monospace; background:var(--surface);
                     color:var(--text); border:1px solid var(--border);
                     border-radius:8px; padding:10px; font-size:0.85rem"></textarea>
    <div style="margin-top:10px; display:flex; align-items:center; gap:12px">
      <button id="policy-save-btn" class="restart-btn" style="display:inline-block">Save policy</button>
      <span id="policy-save-status" class="status"></span>
    </div>
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
let saveTimer = null;
let openGroups = new Set();

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

async function restartAddon() {
  const btn = document.getElementById('restartBtn');
  if (!confirm('Restart the add-on now? The web UI will become unreachable for ~30 seconds.')) return;
  btn.disabled = true;
  btn.textContent = 'Restarting...';
  try {
    const resp = await fetch('./api/settings/restart', {method: 'POST'});
    if (resp.ok) {
      btn.textContent = 'Restart initiated — reload page in ~30s';
    } else {
      let msg = 'Restart failed';
      try {
        const err = await resp.json();
        if (err.error && err.error.message) msg = 'Failed: ' + err.error.message;
      } catch (_e) {}
      btn.textContent = msg;
      btn.disabled = false;
      alert(msg);
    }
  } catch (_e) {
    // Connection lost mid-request is actually expected — the addon is restarting
    btn.textContent = 'Restart initiated (connection dropped)';
  }
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

    // Per-group toggle state: enabled if ANY non-mandatory/non-gated tool is enabled
    const toggleable = tools.filter(t => !MANDATORY.includes(t.name) && !t.disabled_by);
    const anyEnabled = toggleable.some(t => getState(t.name) !== 'disabled');
    const groupEnabled = tools.filter(t => {
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
      const ann = t.annotations || {};
      const isReadOnly = ann.readOnlyHint === true;
      const isDestructive = ann.destructiveHint === true;

      total++;
      if (isFeatureGated) disabledCount++;
      else if (state === 'disabled') disabledCount++;
      else if (state === 'pinned') { enabledCount++; pinnedCount++; }
      else enabledCount++;

      const isEnabled = isFeatureGated ? false : (isMandatory || state !== 'disabled');
      const isPinned = isFeatureGated ? false : (isMandatory || state === 'pinned' || DEFAULT_PINNED.includes(t.name));
      const lockEnabled = isMandatory || isFeatureGated;
      const lockPinned = isMandatory || isFeatureGated || !isEnabled;

      const div = document.createElement('div');
      div.className = 'tool';
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

      div.innerHTML = `<div class="tool-info">` +
        `<div class="tool-name">${escapeHtml(title)}${badges}</div>` +
        `<div class="tool-meta">${escapeHtml(t.name)}</div>` +
        (desc ? `<div class="tool-desc">${escapeHtml(desc)}</div>` : '') +
        gatedNote +
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
        `</div>`;

      const inputs = div.querySelectorAll('input[type="checkbox"]');
      inputs.forEach(input => {
        if (input.disabled) return;
        input.addEventListener('change', (e) => {
          const field = e.target.dataset.field;
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
  } else {
    updateStatus('Save failed!');
  }
}

function updateStatus(text, saved) {
  const el = document.getElementById('status');
  el.textContent = text;
  el.className = saved ? 'status saved' : 'status';
}

document.getElementById('search').addEventListener('input', (e) => {
  const q = e.target.value.toLowerCase();
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
});

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
};

const BACKUP_ORIGIN_LABELS = {
  addon: 'Synced to Supervisor — save will restart the add-on.',
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
    } else {
      const min = f.field === 'auto_backup_throttle_minutes' ? 0 : 1;
      const max = f.field === 'auto_backup_throttle_minutes' ? 1440 : 10000;
      controlHtml = `<input type="number" data-field="${escapeHtml(f.field)}" value="${Number(f.value)}" min="${min}" max="${max}" ${f.editable ? '' : 'disabled'}>`;
    }
    let originMsg;
    if (f.origin === 'env') {
      originMsg = `Set via env var <code>${escapeHtml(f.env_var)}</code> — unset it to edit here.`;
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
    if (data.restarting) {
      statusEl.textContent = 'Saved — addon is restarting. Reload in ~30s.';
    } else {
      statusEl.textContent = 'Saved.';
      btn.disabled = false;
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
  enable_yaml_config_editing: {
    label: "Enable YAML config editing (beta)",
    help: "Beta feature — disabled by default. Allows AI assistants to add, replace, or remove top-level keys in configuration.yaml and packages/*.yaml. Only whitelisted keys are allowed (e.g., template, sensor, command_line, mqtt, knx); core keys like homeassistant, http, and recorder are blocked. Each edit validates YAML syntax, runs a config check, and creates an automatic backup. Changes to most keys require a full HA restart to take effect. See docs/beta.md for known limitations. Dedicated tools (automations, scripts, scenes, helpers, template sensors) should be preferred when available.",
  },
  enable_lite_docstrings: {
    label: "Enable lite tool docstrings (beta)",
    help: "Beta feature — disabled by default. Replaces the docstrings on a handful of heavy ha-mcp tools (automations, scripts, scenes, helpers, dashboards, ha_call_service, ha_config_set_yaml) with shorter variants that defer schema and example detail to the ha_get_skill_guide tool (or its skill:// resource). WARNING: this reduces idle token usage, but may degrade LLM performance — the trimmed descriptions rely on the LLM actually calling the skill tool or reading the skill resource for detail, which is not guaranteed (some models will skip the extra tool call and end up with less guidance than they had before). Best paired with a client that supports MCP resources or with enable_tool_search. Requires restart to take effect.",
  },
  enable_filesystem_tools: {
    label: "Enable filesystem tools (beta)",
    help: "Sets HAMCP_ENABLE_FILESYSTEM_TOOLS=true. Enables direct file read/write access to your Home Assistant filesystem. WARNING: This gives the MCP server sensitive direct file access to your system. Only enable if you trust the AI assistant with file operations. Requires restart to take effect.",
  },
  enable_custom_component_integration: {
    label: "Enable custom component integration (beta)",
    help: "Sets HAMCP_ENABLE_CUSTOM_COMPONENT_INTEGRATION=true. Enables the ha_install_mcp_tools installer tool, which can help install the ha_mcp_tools custom component. This setting does not control whether the MCP server loads or interacts with the custom component, and it is not required for filesystem tools to function. Only enable if you want to allow the AI assistant to use the installer tool. Requires restart to take effect.",
  },
};

const ORIGIN_LOCKED_NOTE = {
  env: 'Set via environment variable — unset it to edit here.',
  addon: 'Managed by the add-on Configuration tab — open Settings → Add-ons → ha-mcp → Configuration to edit.',
};

async function loadFeatureFlags() {
  let resp;
  try {
    resp = await fetch('./api/settings/features');
  } catch (_e) {
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
  } catch (_e) {
    document.getElementById('featuresBody').innerHTML =
      '<div class="feature-row"><div class="feature-help">' +
      'Feature flags response was not valid JSON.</div></div>';
    return;
  }
  renderFeatureFlags(data.flags || {});
}

function renderFeatureFlags(flags) {
  const body = document.getElementById('featuresBody');
  body.innerHTML = '';
  // Render in the order FEATURE_META declares — gives consistent
  // grouping (Tool Search rows together, beta toggles together)
  // regardless of dict iteration order returned by the server.
  Object.keys(FEATURE_META).forEach(fieldName => {
    const f = flags[fieldName];
    if (!f) return;
    const meta = FEATURE_META[fieldName];
    const row = document.createElement('div');
    row.className = 'feature-row' + (f.editable ? '' : ' locked');

    const info = document.createElement('div');
    info.className = 'feature-info';
    const envVarSuffix = f.origin === 'env'
      ? ` (<code>${escapeHtml(f.env_var)}</code>)`
      : '';
    const lockedNote = !f.editable
      ? `<div class="feature-locked-note">` +
        `${escapeHtml(ORIGIN_LOCKED_NOTE[f.origin] || '')}${envVarSuffix}` +
        `</div>`
      : '';
    info.innerHTML =
      `<div class="feature-name">${escapeHtml(meta.label)}</div>` +
      `<div class="feature-help">${escapeHtml(meta.help)}</div>` +
      lockedNote;

    const control = document.createElement('div');
    control.className = 'feature-control';
    if (f.type === 'bool') {
      const label = document.createElement('label');
      label.className = 'switch';
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.checked = !!f.value;
      input.disabled = !f.editable;
      input.addEventListener('change', () =>
        saveFeatureFlag(fieldName, input.checked)
      );
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
    body.appendChild(row);
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
  if (!resp.ok) {
    let msg = `Save failed (HTTP ${resp.status})`;
    try {
      const err = await resp.json();
      if (err.error && err.error.message) msg = 'Save failed: ' + err.error.message;
    } catch (_e) {}
    updateStatus(msg);
    return;
  }
  // Don't toggle the in-tab restartNotice — it lives in panel-tools
  // and would be hidden behind a tab the user isn't on. The page-level
  // status badge (set above) is visible across every tab and the
  // panel sub-header up top already warns "Changes require restart".
  updateStatus('Saved — restart required', true);
}

// ===== Policies tab (issue #966) =====
// Live approval routes (pending/approve/deny) are only available from
// the main server (in-process ApprovalQueue). The sidecar serves
// config GET/PUT but returns 503 for the live endpoints — the UI
// degrades to "Live approvals unavailable in this mode."
async function policyLoadConfig() {
  let resp;
  try {
    resp = await fetch('./api/policy/config');
  } catch (_e) { return; }
  if (!resp.ok) return;
  const p = await resp.json();
  document.getElementById('policy-enabled').checked = !!p.enabled;
  document.getElementById('policy-default-action').value = p.default_action || 'allow';
  document.getElementById('policy-wait-seconds').value = p.wait_seconds ?? 60;
  document.getElementById('policy-ttl-minutes').value = p.approval_ttl_minutes ?? 5;
  document.getElementById('policy-rules-json').value =
    JSON.stringify(p.rules || [], null, 2);
}

async function policySaveConfig() {
  const statusEl = document.getElementById('policy-save-status');
  let rules;
  try {
    rules = JSON.parse(document.getElementById('policy-rules-json').value || '[]');
  } catch (e) {
    statusEl.textContent = 'Invalid JSON in rules: ' + e.message;
    return;
  }
  const body = {
    enabled: document.getElementById('policy-enabled').checked,
    default_action: document.getElementById('policy-default-action').value,
    wait_seconds: parseInt(document.getElementById('policy-wait-seconds').value, 10),
    approval_ttl_minutes: parseInt(document.getElementById('policy-ttl-minutes').value, 10),
    rules: rules,
  };
  let resp;
  try {
    resp = await fetch('./api/policy/config', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
  } catch (e) {
    statusEl.textContent = 'Network error: ' + e.message;
    return;
  }
  if (resp.ok) {
    statusEl.textContent = 'Saved.';
  } else {
    let detail;
    try { detail = (await resp.json()).error || resp.statusText; }
    catch (_e) { detail = resp.statusText; }
    statusEl.textContent = 'Save failed: ' + detail;
  }
}

async function policyLoadPending() {
  const list = document.getElementById('policy-pending-list');
  let resp;
  try {
    resp = await fetch('./api/policy/pending');
  } catch (_e) { return; }
  if (resp.status === 503) {
    list.innerHTML = '<em>Live approvals unavailable in this mode (sidecar). Use the main settings UI.</em>';
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
    '<div style="border:1px solid var(--border); padding:10px; margin:6px 0; border-radius:8px; background:var(--surface)">' +
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
  try {
    await fetch('./api/policy/' + action, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token: token}),
    });
  } catch (_e) {}
  policyLoadPending();
}

document.getElementById('policy-save-btn').addEventListener('click', policySaveConfig);

// Poll for pending approvals every 3s when Policies tab is visible.
setInterval(() => {
  const policiesTab = document.querySelector('.tab[data-panel="policies"]');
  if (policiesTab && policiesTab.classList.contains('active')) {
    policyLoadPending();
  }
}, 3000);

// ===== Tab switching =====
// Generic dispatcher — every .tab button names its target panel via
// data-panel, every .panel has matching id="panel-<name>". Adding a
// new tab is one button + one panel div; no JS change needed.
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t =>
      t.classList.toggle('active', t === tab)
    );
    const target = tab.dataset.panel;
    document.querySelectorAll('.panel').forEach(p =>
      p.classList.toggle('active', p.id === 'panel-' + target)
    );
    if (target === 'backups') { loadBackupConfig(); loadBackups(); }
    if (target === 'policies') { policyLoadConfig(); policyLoadPending(); }
  });
});

loadFeatureFlags();
loadTools();
</script>
</body>
</html>
"""
)


def _build_stub_policy_handlers(*, data_dir: Path) -> dict[str, Any]:
    """Sidecar variant of the per-tool approval handlers (issue #966).

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
            policy = Policy.model_validate(await request.json())
        except (ValidationError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        save_policy(data_dir, policy)
        return JSONResponse({"saved": True})

    async def unavailable(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "error": (
                    "Live approvals are only available from the main settings "
                    "UI, not the stdio sidecar."
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
    }


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
        config = load_tool_config()
        states = config.get("tools", {})
        for name in DEFAULT_PINNED_TOOLS:
            if name not in states:
                states[name] = "pinned"
        return JSONResponse({"tools": tools, "states": states})

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

        return JSONResponse(
            {
                "success": True,
                "disabled": disabled_count,
                "pinned": pinned_count,
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

        endpoint = f"/addons/{target_slug}/restart"
        # Short timeout — when restarting self, the supervisor kills our
        # process during restart so the connection will drop. A connection
        # drop is actually success on that path.
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
        addon = False if is_sidecar else is_running_in_addon()
        return JSONResponse({"is_addon": addon, "is_sidecar": is_sidecar})

    async def _get_feature_flags(_: Request) -> JSONResponse:
        """Return live feature-flag values + per-field origin + editable flag.

        Per-field origin/editable matrix (see
        :func:`config.get_feature_flag_origin`):

            origin = get_feature_flag_origin(env_name)
            editable = origin in ("file", "default")

        ``"addon"`` and ``"env"`` are non-editable from the web UI;
        the user is told which env var (or addon option) to change
        instead. The envelope shape (``flags`` dict of
        ``{value, origin, editable, type, env_var, min?, max?}``
        entries) is intentionally generic so other settings
        surfaces can render rows with the same JS code.
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
                "editable": origin in ("file", "default"),
                "type": ftype.__name__,
                "env_var": env_name,
            }
            if ftype is int:
                bounds = _FEATURE_FLAG_INT_BOUNDS.get(field_name)
                if bounds is not None:
                    entry["min"], entry["max"] = bounds
            flags[field_name] = entry
        return JSONResponse({"flags": flags})

    async def _save_feature_flags(request: Request) -> JSONResponse:
        """Persist UI-edited feature-flag values.

        Only ``editable`` fields (origin = ``file`` or ``default``)
        accept writes; an attempt to write an env-locked or addon-
        locked field returns ``VALIDATION_INVALID_PARAMETER`` so the
        client surfaces the locking source instead of silently
        discarding the change.
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

        # Build the validated override dict. Reject unknown fields and
        # env/addon-locked fields up front so the user gets a precise
        # error instead of a silent no-op.
        known: dict[str, tuple[str, type]] = {
            fname: (ename, ftype) for fname, ename, ftype in FEATURE_FLAG_FIELDS
        }
        new_overrides: dict[str, Any] = {}
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
            if origin not in ("file", "default"):
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
                logger.warning("Existing %s is corrupt: %s", path, exc, exc_info=True)
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
            # we're about to write a dict either way and there's no
            # prior toggle state to preserve from a non-object root.
        existing.update(new_overrides)

        # Atomic write: tmp + rename. ``path.write_text`` is O_TRUNC +
        # write — a crash mid-write leaves an empty/truncated file
        # that the next ``_read_feature_flag_override_file`` call
        # would refuse, losing every prior toggle.
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
        # next ``get_global_settings()`` call (server-restart still
        # required for many flags — the UI surfaces that — but the
        # cached singleton must not return the stale pre-write
        # values to subsequent /api/settings/features GETs).
        _reset_global_settings()
        return JSONResponse({"success": True})

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
            else:
                continue
            clean[field_name] = value
        if not clean:
            return {}, "No editable auto-backup fields in body"
        return clean, None

    async def _save_backup_config(request: Request) -> JSONResponse:
        """Persist auto-backup config edits and publish to the live process.

        Routing:
        - Addon mode: POST ``/addons/self/options`` to update ``config.yaml``,
          then ``/addons/self/restart`` to make the new values take effect via
          ``start.py``'s env-var write at next boot. The HTTP response races
          the restart-induced socket drop; the JS treats both 200 and
          connection-drop as success and reloads the page after ~30s.
        - Standalone (file) mode: refuse any field that's pinned by an env
          var (process or ``.env``) — return 409 with the offending names so
          the UI can refresh and show the read-only banner. Editable fields
          merge into ``<data_dir>/backup_settings.json`` and a Settings
          cache reset publishes them immediately (no restart).
        """
        try:
            payload = await request.json()
        except (ValueError, json.JSONDecodeError):
            return _bad_request("Invalid JSON body")
        clean, err = _validate_backup_payload(payload)
        if err is not None:
            return _bad_request(err)

        if is_running_in_addon():
            if not os.environ.get("SUPERVISOR_TOKEN"):
                return JSONResponse(
                    create_error_response(
                        ErrorCode.CONFIG_VALIDATION_FAILED,
                        "Supervisor token missing — cannot update add-on options",
                    ),
                    status_code=400,
                )
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
            options_body = {"options": clean}
            try:
                async with make_supervisor_httpx_client(
                    timeout=10.0, verify=server.settings.verify_ssl
                ) as sclient:
                    opt_resp = await sclient.post(
                        "/addons/self/options", json=options_body
                    )
            except httpx.HTTPError as exc:
                logger.warning(
                    "Failed to PUT /addons/self/options: %s", exc, exc_info=True
                )
                return JSONResponse(
                    create_error_response(
                        ErrorCode.CONNECTION_FAILED,
                        f"Supervisor options update failed: {exc}",
                    ),
                    status_code=502,
                )
            if opt_resp.status_code >= 400:
                body = opt_resp.text[:400]
                logger.warning(
                    "Supervisor rejected options update (%s): %s",
                    opt_resp.status_code,
                    body,
                )
                return JSONResponse(
                    create_error_response(
                        ErrorCode.CONFIG_VALIDATION_FAILED,
                        f"Supervisor rejected options update: {body}",
                    ),
                    status_code=opt_resp.status_code,
                )
            # Trigger restart so start.py rewrites env vars from config.yaml.
            # Socket may drop mid-response (self-restart) — treat that as
            # success, same as the existing _restart_addon handler.
            try:
                async with make_supervisor_httpx_client(
                    timeout=5.0, verify=server.settings.verify_ssl
                ) as sclient:
                    await sclient.post("/addons/self/restart")
            except (httpx.ReadError, httpx.RemoteProtocolError):
                pass
            except httpx.HTTPError as exc:
                logger.warning(
                    "Options updated but restart request failed: %s", exc, exc_info=True
                )
                return JSONResponse(
                    {
                        "success": True,
                        "applied": clean,
                        "mode": "addon",
                        "warning": (
                            "Options saved to config.yaml but restart request "
                            "failed; restart the add-on manually to apply."
                        ),
                    },
                    status_code=200,
                )
            return JSONResponse(
                {
                    "success": True,
                    "applied": clean,
                    "mode": "addon",
                    "restarting": True,
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
        _reset_global_settings()
        return JSONResponse(
            {
                "success": True,
                "applied": clean,
                "mode": "file",
                "restarting": False,
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
        "list_backups": _list_backups,
        "view_backup": _view_backup,
        "diff_backup": _diff_backup,
        "restore_backup": _restore_backup,
        "delete_backup": _delete_backup,
        "delete_backups_bulk": _delete_backups_bulk,
        "get_backup_config": _get_backup_config,
        "save_backup_config": _save_backup_config,
    }

    # Per-tool approval policy (issue #966). The main server attaches an
    # ApprovalQueue to the server object once PolicyMiddleware is wired
    # in (Task 5.2). Only the main server can serve the live
    # pending/approve/deny endpoints because the queue is in-memory; the
    # sidecar (or a main server without the queue attribute yet) falls
    # back to stub handlers that serve config GET/PUT and return 503 for
    # live approval routes.
    approval_queue = (
        getattr(server, "approval_queue", None) if server is not None else None
    )
    if not is_sidecar and approval_queue is not None:
        from .policy.handlers import build_policy_handlers

        handlers.update(
            build_policy_handlers(
                data_dir=get_data_dir(),
                queue=approval_queue,
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

    # Expose the resolved prefix to the server so the per-tool approval
    # middleware can build absolute-looking approval URLs (#966). The
    # middleware reads this lazily via ``getattr(self,
    # "_settings_secret_prefix", "")`` so the closure picks up the value
    # set here, even though ``_apply_per_tool_approval`` ran in __init__
    # before this function was called.
    server._settings_secret_prefix = secret_prefix  # noqa: SLF001

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
        # Per-tool approval policy endpoints (#966)
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
        # Per-tool approval policy endpoints (#966)
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
