"""Web-based settings UI for tool visibility configuration.

Serves a self-contained HTML page at /settings that lets users enable,
disable, and pin MCP tools. Changes apply immediately without server
restart. Persists to a JSON config file alongside the MCP server data.

Works across all installation methods (add-on, Docker, standalone).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from .transforms import DEFAULT_PINNED_TOOLS

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from .server import HomeAssistantSmartMCPServer

logger = logging.getLogger(__name__)

MANDATORY_TOOLS: set[str] = {
    "ha_search_entities",
    "ha_get_overview",
    "ha_get_state",
    "ha_report_issue",
}


def _get_config_path() -> Path:
    """Return the path to the tool config JSON file."""
    data_dir = Path("/data")
    if data_dir.exists():
        return data_dir / "tool_config.json"
    home_dir = Path.home() / ".ha-mcp"
    home_dir.mkdir(parents=True, exist_ok=True)
    return home_dir / "tool_config.json"


def load_tool_config(settings: Any = None) -> dict[str, Any]:
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


def _find_tools_json() -> Path | None:
    """Locate tools.json, checking multiple paths for different install methods."""
    candidates = [
        Path(__file__).parent.parent.parent / "site" / "src" / "data" / "tools.json",
        Path("/app/site/src/data/tools.json"),
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _get_tool_metadata(server: HomeAssistantSmartMCPServer) -> list[dict[str, Any]]:
    """Extract metadata for all registered tools from the server."""
    tools_json = _find_tools_json()
    if tools_json:
        try:
            result: list[dict[str, Any]] = json.loads(tools_json.read_text())
            return result
        except (OSError, json.JSONDecodeError):
            pass

    tools: list[dict[str, Any]] = []
    for tool in server.mcp._tool_manager._tools.values():  # type: ignore[attr-defined]
        tags = list(tool.tags) if tool.tags else []
        annotations: dict[str, bool] = {}
        if tool.annotations:
            if hasattr(tool.annotations, "readOnlyHint"):
                annotations["readOnlyHint"] = tool.annotations.readOnlyHint or False
            if hasattr(tool.annotations, "destructiveHint"):
                annotations["destructiveHint"] = tool.annotations.destructiveHint or False
        tools.append({
            "name": tool.name,
            "title": (tool.annotations.title if tool.annotations and hasattr(tool.annotations, "title") else "") or tool.name,
            "description": (tool.description or "")[:200],
            "tags": tags,
            "annotations": annotations,
        })
    tools.sort(key=lambda t: (t["tags"][0] if t["tags"] else "zzz", t["name"]))
    return tools


def apply_tool_visibility(
    mcp: FastMCP,
    config: dict[str, Any],
    settings: Any,
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
    padding: 12px 16px; cursor: pointer; user-select: none; }
  .group-header:hover { background: var(--surface-hover); }
  .group-name { font-weight: 600; font-size: 0.95rem; }
  .group-count { font-size: 0.8rem; color: var(--text-secondary); margin-left: 8px; }
  .group-chevron { transition: transform 0.2s; color: var(--text-secondary); }
  .group-chevron.open { transform: rotate(90deg); }
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
  .tool-select { min-width: 140px; padding: 6px 10px; border-radius: 8px;
    border: 1px solid var(--border); background: var(--surface); color: var(--text);
    font-size: 0.85rem; cursor: pointer; }
  .tool-select:disabled { opacity: 0.4; cursor: not-allowed; background: var(--disabled-bg); }
  .tool-select option { background: var(--surface); }
  .summary { display: flex; gap: 16px; padding: 8px 0; margin-bottom: 16px;
    font-size: 0.85rem; color: var(--text-secondary); flex-wrap: wrap; }
  .summary span { background: var(--surface); padding: 4px 12px; border-radius: 8px; }
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
<div class="summary" id="summary"></div>
<input type="text" class="search" id="search" placeholder="Search tools...">
<div id="groups"></div>
<script>
let toolData = [];
let toolStates = {};
let saveTimer = null;

async function loadTools() {
  const resp = await fetch('/api/settings/tools');
  const data = await resp.json();
  toolData = data.tools;
  toolStates = data.states;
  render();
  updateStatus('Loaded');
}

function getState(name) {
  if (toolStates[name]) return toolStates[name];
  const defs = """ + json.dumps(list(DEFAULT_PINNED_TOOLS)) + """;
  return defs.includes(name) ? 'pinned' : 'enabled';
}

function render() {
  const groups = {};
  toolData.forEach(t => {
    const tag = (t.tags && t.tags[0]) || 'Other';
    if (!groups[tag]) groups[tag] = [];
    groups[tag].push(t);
  });

  const container = document.getElementById('groups');
  container.innerHTML = '';

  let total = 0, enabled = 0, pinned = 0, disabled = 0;

  Object.keys(groups).sort().forEach(tag => {
    const tools = groups[tag];
    const group = document.createElement('div');
    group.className = 'group';

    const header = document.createElement('div');
    header.className = 'group-header';
    const enabledCount = tools.filter(t => getState(t.name) !== 'disabled').length;
    header.innerHTML = `<div><span class="group-name">${tag}</span>` +
      `<span class="group-count">${enabledCount}/${tools.length} enabled</span></div>` +
      `<span class="group-chevron">&#9654;</span>`;
    header.onclick = () => {
      const toolsDiv = group.querySelector('.group-tools');
      const chevron = header.querySelector('.group-chevron');
      toolsDiv.classList.toggle('open');
      chevron.classList.toggle('open');
    };

    const toolsDiv = document.createElement('div');
    toolsDiv.className = 'group-tools';

    tools.forEach(t => {
      const state = getState(t.name);
      const isMandatory = """ + json.dumps(list(MANDATORY_TOOLS)) + """.includes(t.name);
      const ann = t.annotations || {};
      const isReadOnly = ann.readOnlyHint === true;
      const isDestructive = ann.destructiveHint === true;

      total++;
      if (state === 'disabled') disabled++;
      else if (state === 'pinned') pinned++;
      else enabled++;

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

      div.innerHTML = `<div class="tool-info">` +
        `<div class="tool-name">${title}${badges}</div>` +
        `<div class="tool-meta">${t.name}</div>` +
        (desc ? `<div class="tool-desc">${desc}</div>` : '') +
        `</div>` +
        `<select class="tool-select" data-tool="${t.name}" ${isMandatory ? 'disabled' : ''}>` +
        `<option value="enabled" ${state === 'enabled' ? 'selected' : ''}>Enabled</option>` +
        `<option value="pinned" ${state === 'pinned' ? 'selected' : ''}>Pinned</option>` +
        `<option value="disabled" ${state === 'disabled' ? 'selected' : ''}>Disabled</option>` +
        `</select>`;

      const select = div.querySelector('select');
      if (select && !isMandatory) {
        select.addEventListener('change', (e) => {
          toolStates[t.name] = e.target.value;
          scheduleSave();
          render();
        });
      }
      toolsDiv.appendChild(div);
    });

    group.appendChild(header);
    group.appendChild(toolsDiv);
    container.appendChild(group);
  });

  document.getElementById('summary').innerHTML =
    `<span>${total} total</span>` +
    `<span style="color:var(--success)">${enabled} enabled</span>` +
    `<span style="color:var(--accent)">${pinned} pinned</span>` +
    `<span style="color:var(--danger)">${disabled} disabled</span>`;
}

function scheduleSave() {
  clearTimeout(saveTimer);
  updateStatus('Unsaved changes...');
  saveTimer = setTimeout(saveConfig, 800);
}

async function saveConfig() {
  updateStatus('Saving...');
  const resp = await fetch('/api/settings/tools', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({states: toolStates}),
  });
  if (resp.ok) {
    updateStatus('Saved', true);
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
        tools = _get_tool_metadata(server)
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
                {"success": False, "error": {"code": "VALIDATION_ERROR", "message": "Invalid JSON body"}},
                status_code=400,
            )

        states = body.get("states", {})
        config = load_tool_config()
        config["tools"] = states
        save_tool_config(config)

        disabled_names: set[str] = set()
        pinned_names: set[str] = set()

        for name, state in states.items():
            if state == "disabled":
                disabled_names.add(name)
            elif state == "pinned":
                pinned_names.add(name)

        if not server.settings.enable_yaml_config_editing:
            disabled_names.add("ha_config_set_yaml")

        disabled_names -= MANDATORY_TOOLS

        try:
            mcp.enable(names={t["name"] for t in _get_tool_metadata(server)})
            if disabled_names:
                mcp.disable(names=disabled_names)
            mcp.enable(names=MANDATORY_TOOLS)
            logger.info(
                "Applied tool visibility: %d disabled, %d pinned",
                len(disabled_names), len(pinned_names),
            )
        except Exception:
            logger.exception("Failed to apply tool visibility")
            return JSONResponse(
                {"success": False, "error": {"code": "INTERNAL_ERROR", "message": "Failed to apply tool visibility"}},
                status_code=500,
            )

        return JSONResponse({"success": True, "disabled": len(disabled_names), "pinned": len(pinned_names)})
