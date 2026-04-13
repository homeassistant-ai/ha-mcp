"""Web-based settings UI for tool visibility configuration.

Serves a self-contained HTML page at /settings that lets users enable,
disable, and pin MCP tools. Changes apply immediately without server
restart. Persists to a JSON config file alongside the MCP server data.

Works across all installation methods (add-on, Docker, standalone).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from .errors import ErrorCode, create_error_response
from .transforms import DEFAULT_PINNED_TOOLS

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from .config import Settings
    from .server import HomeAssistantSmartMCPServer

_VALID_STATES = frozenset({"enabled", "disabled", "pinned"})

logger = logging.getLogger(__name__)

MANDATORY_TOOLS: set[str] = {
    "ha_search_entities",
    "ha_get_overview",
    "ha_get_state",
    "ha_report_issue",
}

# Tools that exist in the codebase but are only registered when a
# corresponding feature flag/env var is set. When the flag is off, these
# won't appear in local_provider._list_tools(), so we inject stub entries
# into the settings UI with a read-only "disabled by" note.
FEATURE_GATED_TOOLS: dict[str, dict[str, str]] = {
    "ha_config_set_yaml": {
        "title": "Set YAML Config",
        "primary_tag": "System",
        "description": "Add, replace, or remove top-level keys in configuration.yaml or package files.",
        "disabled_by": "enable_yaml_config_editing",
        "destructiveHint": "true",
    },
    "ha_list_files": {
        "title": "List Files",
        "primary_tag": "Files",
        "description": "List files in a directory within the Home Assistant config.",
        "disabled_by": "HAMCP_ENABLE_FILESYSTEM_TOOLS",
        "readOnlyHint": "true",
    },
    "ha_read_file": {
        "title": "Read File",
        "primary_tag": "Files",
        "description": "Read a file from the Home Assistant config directory.",
        "disabled_by": "HAMCP_ENABLE_FILESYSTEM_TOOLS",
        "readOnlyHint": "true",
    },
    "ha_write_file": {
        "title": "Write File",
        "primary_tag": "Files",
        "description": "Write a file to allowed directories in the Home Assistant config.",
        "disabled_by": "HAMCP_ENABLE_FILESYSTEM_TOOLS",
        "destructiveHint": "true",
    },
    "ha_delete_file": {
        "title": "Delete File",
        "primary_tag": "Files",
        "description": "Delete a file from allowed directories.",
        "disabled_by": "HAMCP_ENABLE_FILESYSTEM_TOOLS",
        "destructiveHint": "true",
    },
}


def _get_config_path() -> Path:
    """Return the path to the tool config JSON file."""
    data_dir = Path("/data")
    if data_dir.exists():
        return data_dir / "tool_config.json"
    home_dir = Path.home() / ".ha-mcp"
    home_dir.mkdir(parents=True, exist_ok=True)
    return home_dir / "tool_config.json"


def load_tool_config(settings: Settings | None = None) -> dict[str, Any]:
    """Load persisted tool config, seeding from env vars if no file exists."""
    path = _get_config_path()
    if path.exists():
        try:
            result: dict[str, Any] = json.loads(path.read_text())
            return result
        except (OSError, json.JSONDecodeError):
            logger.warning("Failed to read tool config from %s", path)

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


def save_tool_config(config: dict[str, Any]) -> None:
    """Persist tool config to disk."""
    path = _get_config_path()
    try:
        path.write_text(json.dumps(config, indent=2))
        logger.info("Saved tool config to %s", path)
    except OSError:
        logger.exception("Failed to save tool config to %s", path)


async def _get_tool_metadata(server: HomeAssistantSmartMCPServer) -> list[dict[str, Any]]:
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
        tools.append({
            "name": tool.name,
            "title": title,
            "description": (tool.description or "")[:200],
            "tags": tags,
            "primary_tag": primary,
            "annotations": annotations,
        })

    # Inject stub entries for feature-gated tools that aren't registered
    registered_names = {t["name"] for t in tools}
    for name, meta in FEATURE_GATED_TOOLS.items():
        if name in registered_names:
            continue
        stub_annotations: dict[str, bool] = {}
        if meta.get("readOnlyHint") == "true":
            stub_annotations["readOnlyHint"] = True
        if meta.get("destructiveHint") == "true":
            stub_annotations["destructiveHint"] = True
        tools.append({
            "name": name,
            "title": meta["title"],
            "description": meta["description"],
            "tags": [meta["primary_tag"]],
            "primary_tag": meta["primary_tag"],
            "annotations": stub_annotations,
            "disabled_by": meta["disabled_by"],
        })

    tools.sort(key=lambda t: (t["primary_tag"], t["name"]))
    return tools


def apply_tool_visibility(
    mcp: FastMCP,
    config: dict[str, Any],
    settings: Settings,
) -> set[str]:
    """Apply tool visibility from config, respecting safety toggles.

    Args:
        mcp: The FastMCP instance to enable/disable tools on.
        config: The tool_config.json contents (per-tool states).
        settings: The server Settings (for enable_yaml_config_editing etc.).
    """
    disabled_names: set[str] = set()
    pinned_names: set[str] = set()

    tool_states = config.get("tools", {})
    for name, state in tool_states.items():
        if state == "disabled":
            disabled_names.add(name)
        elif state == "pinned":
            pinned_names.add(name)

    if not settings.enable_yaml_config_editing:
        disabled_names.add("ha_config_set_yaml")
    else:
        disabled_names.discard("ha_config_set_yaml")

    disabled_names -= MANDATORY_TOOLS

    if disabled_names:
        mcp.disable(names=disabled_names)
        logger.info("Disabled tools: %s", ", ".join(sorted(disabled_names)))

    mcp.enable(names=MANDATORY_TOOLS)

    return pinned_names


_SETTINGS_HTML = """\
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
</style>
</head>
<body>
<div class="header">
  <h1>Tool Settings</h1>
  <span id="status" class="status">Loading...</span>
</div>
<div class="readonly-notice">
  Safety toggles (Enable Skills, Tool Search, YAML Config Editing) are managed in the
  add-on configuration page and require a restart to change.
</div>
<div class="pin-notice show" id="pinNotice">
  Pin toggles only take effect when Tool Search is enabled in the add-on
  configuration. Without Tool Search, all enabled tools are always visible
  and pinning has no extra effect.
</div>
<div class="restart-notice" id="restartNotice">
  <span class="restart-notice-text">
    ⚠ Changes saved. Restart the add-on for them to take effect — disabled
    tools will be fully removed from the MCP tool list on next startup.
  </span>
  <button class="restart-btn" id="restartBtn" style="display:none">Restart Add-on</button>
</div>
<div class="summary" id="summary"></div>
<input type="text" class="search" id="search" placeholder="Search tools...">
<div id="groups"></div>
<script>
let toolData = [];
let toolStates = {};
let saveTimer = null;
let openGroups = new Set();

async function loadTools() {
  const resp = await fetch('./api/settings/tools');
  const data = await resp.json();
  toolData = data.tools;
  toolStates = data.states;
  render();
  updateStatus('Loaded');

  // Show restart button if running as add-on
  try {
    const infoResp = await fetch('./api/settings/info');
    const info = await infoResp.json();
    if (info.is_addon) {
      document.getElementById('restartBtn').style.display = '';
    }
  } catch (_e) {}
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

const DEFAULT_PINNED = """ + json.dumps(list(DEFAULT_PINNED_TOOLS)) + """;
const MANDATORY = """ + json.dumps(list(MANDATORY_TOOLS)) + """;

function getState(name) {
  if (toolStates[name]) return toolStates[name];
  return DEFAULT_PINNED.includes(name) ? 'pinned' : 'enabled';
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
      `<span class="group-name">${tag}</span>` +
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
      const gatedNote = disabledBy ? `<div class="disabled-by-note">Requires ${disabledBy} in add-on config</div>` : '';

      div.innerHTML = `<div class="tool-info">` +
        `<div class="tool-name">${title}${badges}</div>` +
        `<div class="tool-meta">${t.name}</div>` +
        (desc ? `<div class="tool-desc">${desc}</div>` : '') +
        gatedNote +
        `</div>` +
        `<div class="tool-toggles">` +
          `<div class="toggle-group">` +
            `<label class="switch"><input type="checkbox" data-tool="${t.name}" data-field="enabled" ` +
              `${isEnabled ? 'checked' : ''} ${lockEnabled ? 'disabled' : ''}>` +
              `<span class="slider"></span></label>` +
            `<span>enabled</span>` +
          `</div>` +
          `<div class="toggle-group ${!isEnabled ? 'disabled-toggle' : ''}">` +
            `<label class="switch"><input type="checkbox" data-tool="${t.name}" data-field="pinned" ` +
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
loadTools();
</script>
</body>
</html>
"""


def register_settings_routes(
    mcp: FastMCP,
    server: HomeAssistantSmartMCPServer,
) -> None:
    """Register the /settings web UI and /api/settings/* endpoints."""

    @mcp.custom_route("/", methods=["GET"])
    async def _root_page(_: Request) -> HTMLResponse:
        return HTMLResponse(_SETTINGS_HTML)

    @mcp.custom_route("/settings", methods=["GET"])
    async def _settings_page(_: Request) -> HTMLResponse:
        return HTMLResponse(_SETTINGS_HTML)

    @mcp.custom_route("/api/settings/tools", methods=["GET"])
    async def _get_tools(_: Request) -> JSONResponse:
        tools = await _get_tool_metadata(server)
        config = load_tool_config()
        states = config.get("tools", {})
        for name in DEFAULT_PINNED_TOOLS:
            if name not in states:
                states[name] = "pinned"
        return JSONResponse({"tools": tools, "states": states})

    @mcp.custom_route("/api/settings/tools", methods=["POST"])
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
        save_tool_config(config)

        disabled_count = sum(1 for s in states.values() if s == "disabled")
        pinned_count = sum(1 for s in states.values() if s == "pinned")
        logger.info(
            "Saved tool config (restart required to apply): %d disabled, %d pinned",
            disabled_count, pinned_count,
        )

        return JSONResponse({
            "success": True,
            "disabled": disabled_count,
            "pinned": pinned_count,
            "restart_required": True,
        })

    @mcp.custom_route("/api/settings/restart", methods=["POST"])
    async def _restart_addon(_: Request) -> JSONResponse:
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            return JSONResponse(
                create_error_response(
                    ErrorCode.CONFIG_VALIDATION_FAILED,
                    "Restart only available when running as an add-on",
                    details="SUPERVISOR_TOKEN environment variable is not set",
                ),
                status_code=400,
            )
        # Short timeout — the supervisor kills our process during restart so
        # the connection will drop. A connection drop is actually success.
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    "http://supervisor/addons/self/restart",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError):
            # Connection dropped mid-request — restart is happening
            logger.info("Restart request connection dropped (expected during restart)")
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
            logger.error("Supervisor restart failed: %d %s", resp.status_code, body)
            return JSONResponse(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    f"Supervisor returned {resp.status_code}: {body[:500]}",
                ),
                status_code=502,
            )
        return JSONResponse({"success": True, "message": "Restart initiated"})

    @mcp.custom_route("/api/settings/info", methods=["GET"])
    async def _settings_info(_: Request) -> JSONResponse:
        return JSONResponse({
            "is_addon": bool(os.environ.get("SUPERVISOR_TOKEN")),
        })
