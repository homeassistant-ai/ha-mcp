"""Web-based settings UI for tool visibility configuration.

Serves a self-contained HTML page at /settings that lets users enable,
disable, and pin MCP tools. Changes apply immediately without server
restart. Persists to a JSON config file alongside the MCP server data.

Works across all installation methods (add-on, Docker, standalone).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, NotRequired, TypedDict

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from ._version import get_version, is_running_in_addon
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


# The settings-UI client script lives in settings.js (a real file for
# editor/JS tooling). It is a template with two sentinel tokens for the
# Python-injected constant lists; substitute them with the same values the
# inline literal used so the rendered HTML is byte-identical. Injected
# inline (not served) -- the serving model is unchanged.
#
# This is a module-import-time file read: importing settings_ui (done by
# server.py / __main__.py / the sidecar) now depends on settings.js being
# present. If packaging drops it, fail with a packaging-specific ImportError
# rather than a bare FileNotFoundError so the cause is obvious.
_SETTINGS_JS_PATH = Path(__file__).parent / "settings.js"
try:
    _settings_js_template = _SETTINGS_JS_PATH.read_text(encoding="utf-8")
except FileNotFoundError as exc:  # pragma: no cover - packaging guard
    raise ImportError(
        f"settings.js missing at {_SETTINGS_JS_PATH}. It must ship in "
        "package-data (wheel), MANIFEST.in (sdist), and the PyInstaller datas "
        "(binary) -- this is a packaging bug, not a usage error."
    ) from exc
# str.replace() silently no-ops on an absent token, and a *renamed* sentinel
# (e.g. PINNED_DEFAULTS) slips past both the "__HA_MCP_" not-in test and the
# node --check parse guard -- `const DEFAULT_PINNED = PINNED_DEFAULTS;` is valid
# JS (only a runtime ReferenceError), so a drifted settings.js would ship a
# broken page green. Assert both sentinels are present before substituting.
for _sentinel in ("__HA_MCP_DEFAULT_PINNED__", "__HA_MCP_MANDATORY__"):
    if _sentinel not in _settings_js_template:
        raise ImportError(
            f"settings.js is out of sync: sentinel {_sentinel} not found. "
            "The Python injection and settings.js have drifted."
        )
# sorted(), not list(): DEFAULT_PINNED_TOOLS / MANDATORY_TOOLS are sets, so
# json.dumps(list(...)) is per-process-ordered -- the only reason proving the
# original extraction byte-identical needed PYTHONHASHSEED pinned. sorted()
# makes the two injected arrays deterministic across processes.
_SETTINGS_JS = _settings_js_template.replace(
    "__HA_MCP_DEFAULT_PINNED__", json.dumps(sorted(DEFAULT_PINNED_TOOLS))
).replace("__HA_MCP_MANDATORY__", json.dumps(sorted(MANDATORY_TOOLS)))


# The settings-UI CSS lives in settings.css, extracted the same way as
# settings.js. Unlike the JS it has no Python injection points -- a plain
# read, no token substitution -- and is injected inline between the same
# <style>/</style> tags so the served page stays byte-identical. It carries
# the same import-time packaging dependency as settings.js, so the same
# FileNotFoundError -> ImportError packaging guard applies.
_SETTINGS_CSS_PATH = Path(__file__).parent / "settings.css"
try:
    _SETTINGS_CSS = _SETTINGS_CSS_PATH.read_text(encoding="utf-8")
except FileNotFoundError as exc:  # pragma: no cover - packaging guard
    raise ImportError(
        f"settings.css missing at {_SETTINGS_CSS_PATH}. It must ship in "
        "package-data (wheel), MANIFEST.in (sdist), and the PyInstaller datas "
        "(binary) -- this is a packaging bug, not a usage error."
    ) from exc


_SETTINGS_HTML = (
    """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>HA-MCP Tool Settings</title>
<!-- Empty data URI: tells the browser "no favicon" so it never requests
     /favicon.ico (which would 404 and log a console error, since the
     settings server serves no such asset in any deployment mode). -->
<link rel="icon" href="data:,">
<style>"""
    + _SETTINGS_CSS
    + """</style>
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

  <!-- Two-step note + top Save button. The Save +
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

  <!-- Advanced settings sections. The "Connection
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

  <!-- Beta features sit at the bottom of the panel — these can damage
       the HA system, so they come last and the user sees safer
       settings first. -->
  <h3 class="adv-section-title beta-section-title">Beta features (dangerous)</h3>
  <div id="betaBody"></div>

  <!-- Bottom Save row sits AFTER the beta block (and any nested
       code-mode sub-numerics) so a user editing those doesn't have
       to scroll back up past their own changes. -->
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
<script>"""
    + _SETTINGS_JS
    + """</script>
<footer id="versionFooter" class="version-footer">
  <span id="versionFooterText"></span>
</footer>
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
        # env-pinned rows back unchanged. Previously every save with
        # DISABLED_TOOLS / PINNED_TOOLS non-empty failed because the JS
        # POSTs the whole ``toolStates`` map.
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
                    suggestions=[
                        "Unset the DISABLED_TOOLS / PINNED_TOOLS environment "
                        "variables (or remove them from your addon/Docker "
                        "config), then restart to edit these tools from the UI.",
                    ],
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
        # ``version`` surfaces the running ha-mcp version in the UI
        # footer. ``get_version`` handles the env-override → package
        # metadata → fallback chain itself, so a read per request is
        # cheap. ``HA_MCP_BUILD_VERSION`` is set by both stable and
        # dev addon Dockerfiles, so the version reads correctly on
        # either channel; standalone Docker / uvx falls back to the
        # installed package's metadata.
        try:
            version = get_version()
        except Exception:  # pragma: no cover — defensive only
            logger.warning("get_version() raised; omitting version from info")
            version = None
        return JSONResponse(
            {
                "is_addon": addon,
                "is_sidecar": is_sidecar,
                "instance_id": _PROCESS_INSTANCE_ID,
                "started_at": _PROCESS_STARTED_AT,
                "version": version,
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
                # owns the env.
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

        # Master beta-gate check: a sub-flag write is only valid
        # when the master ``enable_beta_features`` is on AFTER the
        # merge. Derive the post-merge master from the payload (if
        # present), otherwise fall back to the live ``Settings`` value.
        # Reject sub-flag writes that try to enable a beta when the
        # resulting master state would still be off — the runtime gate
        # would force them False anyway and the user should know the
        # save was a no-op rather than learning at next startup.
        #
        # Applied in BOTH standalone and addon mode.
        # The earlier "skip in addon mode" carve-out existed because
        # start.py used to auto-write ENABLE_BETA_FEATURES=true from
        # any beta sub-flag presence; that path is now demoted to a
        # one-cycle legacy bridge. On dev addon, start.py writes the
        # master env var from the schema-bound options key. On stable
        # addon, the master is not in schema and the standalone
        # web-UI master path remains the gate (the gate read below
        # falls through to the override-file value). Either way the
        # gate is sound to apply uniformly.
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
                    suggestions=[
                        "Include enable_beta_features=true in the same save "
                        "payload as the sub-flag(s).",
                        "Or turn on the master 'Enable beta features' toggle "
                        "first, then enable the sub-flag(s).",
                    ],
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

        # Master-off no longer cascades into sub-flag values. The
        # runtime master gate in ``_apply_feature_flag_overrides``
        # continues to force every
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
                            suggestions=[
                                "Check the Home Assistant Supervisor logs and "
                                "the add-on logs for the underlying failure.",
                                "Report this at "
                                "https://github.com/homeassistant-ai/ha-mcp/issues "
                                "if it persists — this indicates an internal bug.",
                            ],
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
        # two overlapping requests can't interleave their RMW. Lock is
        # held only for the read+merge+atomic-write window; pure
        # validation above does not need it.
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
        origin + editable flag.

        Mirrors ``_get_feature_flags`` / ``_get_backup_config`` but for
        the ``ADVANCED_SETTINGS_FIELDS`` registry. Most advanced fields
        write to ``feature_flags.json`` via the shared override file in
        either deployment mode. ``ADDON_SYNCED_ADVANCED_FIELDS``
        (currently ``backup_hint``, ``verify_ssl``) are an exception:
        in addon mode they have ``origin="addon"`` (editable) and saves
        route through Supervisor ``/addons/self/options`` so the addon
        Configuration tab and the web UI share state.
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
        Configuration tab and this web UI share state. Other
        env-pinned fields stay ``'env'`` (locked).

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
        """Persist UI-edited advanced settings.

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
            # Supervisor instead of the override file.
            is_addon_synced = addon_mode and fname in ADDON_SYNCED_ADVANCED_FIELDS
            if is_addon_synced:
                addon_writes_present = True
            elif os.environ.get(env_name) is not None:
                # Add-on mode has no env-var surface for non-schema keys
                # (e.g. CODE_MODE_SAVED_TOOLS_PATH, set by start.py), so
                # "unset it to edit here" is unactionable there — give
                # add-on-aware copy instead of implying a lever exists.
                if addon_mode:
                    message = (
                        f"{fname!r} is fixed by the add-on runtime and "
                        "cannot be changed from the web UI."
                    )
                    suggestions = [
                        "This value is baked into the add-on and is not "
                        "exposed as an editable setting.",
                    ]
                else:
                    message = (
                        f"{fname!r} is set via {env_name} env var — "
                        "unset it to edit here."
                    )
                    suggestions = [
                        f"Unset the {env_name} environment variable (or "
                        "remove it from your Docker config), then restart "
                        "to edit this setting from the UI.",
                    ]
                return JSONResponse(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        message,
                        suggestions=suggestions,
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
                        suggestions=[
                            f"Provide a value for {fname} within the range "
                            f"{bounds[0]}–{bounds[1]}.",
                        ],
                    ),
                    status_code=400,
                )
            choices = _ADVANCED_SETTINGS_CHOICES.get(fname)
            if choices is not None and coerced not in choices:
                return JSONResponse(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"{fname!r} must be one of {list(choices)} (got {coerced!r}).",
                        suggestions=[
                            f"Set {fname} to one of: {', '.join(map(str, choices))}.",
                        ],
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
        # instead of routing them through different sinks.
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
                        suggestions=[
                            "Submit addon-synced fields (e.g. backup_hint, "
                            "verify_ssl) and override-file fields in separate "
                            "save requests.",
                        ],
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
                            suggestions=[
                                "Check the Home Assistant Supervisor logs and "
                                "the add-on logs for the underlying failure.",
                                "Report this at "
                                "https://github.com/homeassistant-ai/ha-mcp/issues "
                                "if it persists — this indicates an internal bug.",
                            ],
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
        # concurrent feature-flag saves on the same file.
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
                suggestions=[
                    f"Send {fname} as a {ftype.__name__} value.",
                ],
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


# Home Assistant proxies every ingress request ("Open Web UI") from the
# Supervisor's fixed network address. Per the add-on ingress contract
# (https://developers.home-assistant.io/docs/add-ons/presentation/#ingress —
# "Only connections from 172.30.32.2 must be allowed") the app must reject
# every other source. This holds under host_network too: ingress proxies to
# http://{app.ip_address}:{ingress_port}/, and for a host-network add-on
# app.ip_address is the hassio bridge gateway 172.30.32.1 — the DESTINATION the
# Supervisor dials (supervisor/docker/app.py ip_address(): host_network ->
# network.gateway). The Supervisor opens that connection from its own container
# address 172.30.32.2, so the transport peer the add-on sees is 172.30.32.2 for
# genuine ingress and some other address (a LAN host, the cloudflared tunnel at
# 172.30.33.x, another add-on) for a direct port-9583 hit. Verified live via
# netstat during an "Open Web UI" click.
SUPERVISOR_INGRESS_IP = "172.30.32.2"

# A settings-UI route handler: async (Request) -> Response.
_SettingsRoute = Callable[[Request], Awaitable[Response]]


def _ingress_only(handler: _SettingsRoute) -> _SettingsRoute:
    """Wrap a root-mounted add-on route so only HA ingress can reach it.

    Add-on root routes carry no MCP secret, so without this guard a direct
    caller on the published port — a LAN peer, a reverse proxy / tunnel
    forwarding the bare root, or a CSRF POST from a LAN browser — could
    rewrite tool config, flip the tool-security-policy, or restart the
    add-on with no authentication. We gate on the *transport* peer
    (``request.client.host``), never ``X-Forwarded-For`` (which a caller can
    forge). The same handlers stay reachable under ``secret_prefix``, where
    the MCP secret path is the auth for direct/remote access.
    """

    @functools.wraps(handler)
    async def _guarded(request: Request) -> Response:
        peer = request.client.host if request.client else None
        if peer != SUPERVISOR_INGRESS_IP:
            logger.warning(
                "Blocked non-ingress request to add-on root route %s from "
                "peer %r (only the Supervisor at %s may reach root routes; "
                "use the MCP secret path for direct/remote access).",
                request.url.path,
                peer,
                SUPERVISOR_INGRESS_IP,
            )
            return JSONResponse(
                {
                    "error": (
                        "This endpoint is only reachable through Home "
                        "Assistant ingress. For direct or remote access, use "
                        "the settings UI under your MCP secret path."
                    )
                },
                status_code=403,
            )
        return await handler(request)

    return _guarded


# Mount prefix the settings UI is served under in long-lived HTTP transports
# (Docker / standalone ha-mcp-web / OAuth / the add-on's secret-path mount).
# Recorded by register_settings_routes so ha_get_overview can point users at
# the settings page in modes that have no stdio sidecar URL file to surface
# (issue #1458). Stays None in pure stdio mode, where the sidecar writes
# ~/.ha-mcp/ui.url instead.
_http_settings_prefix: str | None = None


def get_http_settings_prefix() -> str | None:
    """Return the settings-UI mount prefix for HTTP transports, or None.

    Set by :func:`register_settings_routes` when the page is mounted on a
    long-lived HTTP server. ``ha_get_overview`` reads it to hint at the
    settings page when there is no stdio sidecar URL to hand the user.
    """
    return _http_settings_prefix


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

    # Every route this function mounts except the add-on-only root mount is defined
    # once in this table and mounted under each active prefix below: at root
    # in add-on mode (so HA ingress can proxy localhost:9583/), and under the
    # secret path when one is set (Docker / standalone direct access). A
    # deployment hits either, both, or — guarded above — neither. Deriving
    # the mounts from one table keeps them from drifting; the frontend uses
    # relative fetches (./api/settings/...) so the handlers work at any prefix.
    routes: list[tuple[str, list[str], str]] = [
        ("/settings", ["GET"], "settings_page"),
        ("/api/settings/tools", ["GET"], "get_tools"),
        ("/api/settings/tools", ["POST"], "save_tools"),
        ("/api/settings/restart", ["POST"], "restart_addon"),
        ("/api/settings/info", ["GET"], "settings_info"),
        ("/api/settings/features", ["GET"], "get_feature_flags"),
        ("/api/settings/features", ["POST"], "save_feature_flags"),
        # Advanced settings endpoints
        ("/api/settings/advanced", ["GET"], "get_advanced_settings"),
        ("/api/settings/advanced", ["POST"], "save_advanced_settings"),
        # Auto-backup endpoints (#1288)
        ("/api/settings/backups", ["GET"], "list_backups"),
        ("/api/settings/backups", ["DELETE"], "delete_backups_bulk"),
        ("/api/settings/backups/{name}", ["GET"], "view_backup"),
        ("/api/settings/backups/{name}/diff", ["GET"], "diff_backup"),
        ("/api/settings/backups/{name}/restore", ["POST"], "restore_backup"),
        ("/api/settings/backups/{name}", ["DELETE"], "delete_backup"),
        ("/api/settings/backup-config", ["GET"], "get_backup_config"),
        ("/api/settings/backup-config", ["POST"], "save_backup_config"),
        # Tool security policies endpoints
        ("/api/policy/config", ["GET"], "policy_get_config"),
        ("/api/policy/config", ["PUT"], "policy_put_config"),
        ("/api/policy/pending", ["GET"], "policy_get_pending"),
        ("/api/policy/approve", ["POST"], "policy_post_approve"),
        ("/api/policy/deny", ["POST"], "policy_post_deny"),
        ("/api/policy/tool-schema", ["GET"], "policy_get_tool_schema"),
        ("/api/policy/value-source", ["GET"], "policy_get_value_source"),
    ]

    def _mount(prefix: str, *, guard: bool = False) -> None:
        # guard=True wraps each handler in _ingress_only so the route only
        # answers HA ingress (the Supervisor) — used for the add-on root
        # mount, whose port 9583 is reachable without the MCP secret.
        for path, methods, handler_key in routes:
            handler = handlers[handler_key]
            if guard:
                handler = _ingress_only(handler)
            mcp.custom_route(f"{prefix}{path}", methods=methods)(handler)

    if is_addon:
        # Root mount lets HA ingress proxy localhost:9583/ → the settings UI
        # ("Open Web UI" button). The published port 9583 also makes these
        # routes reachable by direct callers that present no MCP secret, so
        # the root mount is gated with _ingress_only: only the Supervisor
        # (HA ingress, 172.30.32.2) may reach root; every other caller gets
        # 403 and must use the secret-path mount below. The "Open Web UI"
        # button is unaffected — its traffic arrives from the Supervisor.
        mcp.custom_route("/", methods=["GET"])(_ingress_only(handlers["root_page"]))
        _mount("", guard=True)

    if secret_prefix:
        # Mount under the MCP secret path so Docker / standalone clients
        # need the same secret to reach the UI as they do for the MCP
        # endpoint.
        _mount(secret_prefix)
        # Record the mount so ha_get_overview can point users at the settings
        # page in HTTP transports that have no stdio sidecar URL file (#1458).
        global _http_settings_prefix
        _http_settings_prefix = secret_prefix
