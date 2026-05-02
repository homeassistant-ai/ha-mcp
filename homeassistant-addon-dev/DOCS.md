# Home Assistant MCP Server (Dev Channel) - Documentation

**WARNING: This is the development channel. Expect bugs and breaking changes.**

This add-on receives updates with every commit to master. For stable releases, use the main "Home Assistant MCP Server" add-on.

## Configuration

The dev add-on uses the same configuration as the stable version. See the main add-on's documentation for full details.

### Options

| Option | Description | Default |
|--------|-------------|---------|
| `backup_hint` | Backup strength preference | `normal` |
| `secret_path` | Custom secret path (optional) | auto-generated |
| `enable_skills` | Serve bundled HA best-practice skills as MCP resources | `true` |
| `enable_skills_as_tools` | Expose skills via list_resources/read_resource tools | `true` |
| `enable_tool_search` | Replace full tool catalog with search-based discovery (~46K → ~5K tokens) | `false` |
| `enable_yaml_config_editing` *(beta)* | Enables `ha_config_set_yaml` for editing `configuration.yaml` directly. Requires `ha_mcp_tools` custom component. | `false` |
| `enable_filesystem_tools` *(beta)* | Enables file read/write tools (`ha_list_files`, `ha_read_file`, `ha_write_file`, `ha_delete_file`). Requires `ha_mcp_tools` custom component. | `false` |
| `enable_custom_component_integration` *(beta)* | Enables `ha_install_mcp_tools` installer tool for the `ha_mcp_tools` custom component. | `false` |
| `tool_search_max_results` | Max results from `ha_search_tools` (range 2-10) | `5` |
| `disabled_tools` | Comma-separated list of tool names to disable (seed value; web UI is primary) | empty |
| `pinned_tools` | Comma-separated list of tool names to pin when tool search is enabled (seed value; web UI is primary) | empty |

Beta options are hidden under "Show unused optional configuration options" in the add-on Configuration tab. See [beta.md](https://github.com/homeassistant-ai/ha-mcp/blob/master/docs/beta.md) for details.

## Tool Settings Web UI

The add-on exposes a web-based settings page for managing which tools are available to AI assistants. Click **"Open Web UI"** on the add-on info page to access it.

Features:
- **Enable/disable individual tools** — toggle each tool on or off
- **Pin tools** — keep tools always visible when `enable_tool_search` is on
- **Per-group master toggle** — enable/disable all tools in a group (HACS, System, etc.) with one click
- **Search** — filter tools by name or title
- **Mandatory tools** — `ha_search_entities`, `ha_get_overview`, `ha_get_state`, `ha_report_issue` are always enabled and cannot be disabled
- **Feature-gated tools** — `ha_config_set_yaml` (requires `enable_yaml_config_editing`), filesystem tools (require `enable_filesystem_tools`), and `ha_install_mcp_tools` (requires `enable_custom_component_integration`) appear in the list with a note if their feature flag is off
- **In-UI restart** — a "Restart Add-on" button appears after saving to apply changes with one click

**Important:** Tool configuration changes require an add-on restart to take effect. The UI will prompt you to restart after saving.

### Non-add-on installations

In Docker (`ha-mcp-web`) and standalone HTTP installations, the settings UI is mounted under your MCP secret path. Open `http://<host>:<port>/<secret_path>/settings` (the same URL prefix that protects your MCP endpoint). This keeps the auth posture consistent — anyone who can reach your MCP endpoint can also use the settings UI; anyone who can't, can't.

### Text-field fallback

If you prefer not to use the web UI (or want to set these before first start), the `disabled_tools` and `pinned_tools` options accept comma-separated tool names as seed values. On first start, the add-on creates `/data/tool_config.json` from these values. After that, the web UI is the source of truth.

## Updates

The dev channel updates automatically with every commit to master. You may receive multiple updates per day.

To check for updates:
1. Go to Settings > Add-ons
2. Click on "Home Assistant MCP Server (Dev)"
3. Click "Check for updates"

## Switching to Stable

If you want to switch back to stable releases:
1. Uninstall this dev add-on
2. Install the main "Home Assistant MCP Server" add-on

Your configuration will need to be reconfigured.

## Reporting Issues

When reporting issues from the dev channel, please include:
- The commit SHA (shown in the add-on info)
- Steps to reproduce
- Any error logs from the add-on

Issues: https://github.com/homeassistant-ai/ha-mcp/issues
