# Home Assistant MCP Server (Dev Channel) - Documentation

**WARNING: This is the development channel. Expect bugs and breaking changes.**

This app (add-on) receives updates with every commit to master. For stable releases, use the main "Home Assistant MCP Server" app.

## Configuration

The dev app uses the same configuration as the stable version. See the main app's documentation for full details.

### Options

| Option | Description | Default |
|--------|-------------|---------|
| `backup_hint` | Backup strength preference | `normal` |
| `enable_snapshot_actions` | Allow full HA snapshot actions through `ha_manage_backup` and the equivalent `ha_call_service` backup calls; deletion is only available through `ha_manage_backup`, also requires `enable_snapshot_delete`, and its protections still apply. Off blocks listing too. Save and restart to apply. | `true` |
| `backup_read_only` | Allow edit-backup reads and snapshot listing when snapshot actions are enabled; block manual create, restore (including edit restores), and delete, including the equivalent `ha_call_service` backup calls. Automatic pre-edit backups continue. Save and restart to apply. | `false` |
| `secret_path` | Custom secret path (optional) | auto-generated |
| `enable_tool_search` | Replace full tool catalog with search-based discovery (cuts idle context by ~90%, to ~5K tokens). ⚠️ Do NOT enable in clients with built-in tool search / deferred tools (claude.ai, Claude Desktop, Claude Code) — the layers conflict; use the client's built-in search instead. | `false` |
| `enable_tool_security_policies` | Gate high-stakes tool calls (lock/alarm control, automation writes, etc.) behind user approval. Guarded calls block until the user clicks Approve in the Tool Security Policies tab of the web UI. Per-tool rules with optional argument conditions are configured in that same tab. | `false` |
| `read_only_mode` | Toggles all write tools off, and removes ability for tools to make any write or destructive calls. Mixed read/write tools (backups, apps, energy preferences, voice pipelines, and code mode when enabled) stay available with their write operations blocked. Same toggle as the web UI Tools tab. | `false` |
| `redact_secrets` | Redacts secrets from tool responses before they reach the AI assistant: app options and integration fields marked as passwords become set/empty markers, and other responses are scrubbed of secret values the server has already seen (values shorter than 6 characters are skipped). Schema-driven — unmarked fields cannot be detected. | `false` |
| `enable_beta_features` *(master)* | Master gate for the beta sub-flags below. Sub-flags are ignored at runtime while this is off — even when explicitly set to true. Mirrored to the web settings UI under "Beta features (dangerous)". | `true` |
| `enable_yaml_config_editing` *(beta)* | Enables `ha_config_set_yaml` for editing `configuration.yaml` directly. Requires `ha_mcp_tools` custom component. Gated by the master above. | `false` |
| `enable_filesystem_tools` *(beta)* | Enables file read/write tools (`ha_list_files`, `ha_read_file`, `ha_write_file`, `ha_delete_file`). Requires `ha_mcp_tools` custom component. Gated by the master above. | `false` |
| `enable_yaml_edit_confirm` *(beta)* | The first `ha_config_set_yaml` call returns a diff of the change and a confirm token and writes nothing; the edit lands only when the call is repeated with the token. Turn off only to save one round trip per edit. Gated by the master above. | `true` |
| `enable_yaml_packages_automation` *(beta)* | Lets `ha_config_set_yaml` write `automation` inside `packages/*.yaml`. Takes effect only when `enable_yaml_config_editing` is on. Gated by the master above. | `false` |
| `enable_yaml_packages_script` *(beta)* | Lets `ha_config_set_yaml` write `script` inside `packages/*.yaml`. Takes effect only when `enable_yaml_config_editing` is on. Gated by the master above. | `false` |
| `enable_yaml_packages_scene` *(beta)* | Lets `ha_config_set_yaml` write `scene` inside `packages/*.yaml`. Takes effect only when `enable_yaml_config_editing` is on. Gated by the master above. | `false` |
| `enable_code_mode` *(beta)* | Enables `ha_manage_custom_tool`, which lets the AI create, run and save custom Python code in a sandbox when no built-in tool fits. Saved tools persist to `/data/saved_tools.json`. Gated by the master above. | `false` |
| `enable_lite_docstrings` *(beta)* | Replaces the descriptions of 15 large tools with short ones that point to `ha_get_skill_guide`. Saves idle tokens, but models that skip the skill get less guidance. Gated by the master above. | `false` |
| `enable_dashboard_screenshot` *(beta)* | Adds `ha_get_dashboard_screenshot` and screenshot options on the dashboard tools. Needs a separate rendering engine (the "Puppet" app). Gated by the master above. | `false` |
| `enable_security_policy_tool` | Registers `ha_manage_security_policy`, which lets AI agents read and rewrite the tool security policies, including removing approval gates. | `false` |
| `enable_mandatory_bps` | Attaches the best-practice reference files to every successful write by the six config write tools. Turn off only for models with very small context windows. | `true` |
| `enable_strict_mandatory_bps` | Blocks the six best-practice write tools until the client passes back the acknowledgment key from `ha_get_skill_guide`. No effect while `enable_mandatory_bps` is off. | `true` |
| `ha_tool_concurrency` | Limit on Home Assistant tool calls running at once across all sessions (range 0-32). `0` means no limit. A waiting call fails after 60 seconds. | `0` |
| `enable_auto_backup` | Saves a snapshot of an entity before each write or delete tool call changes it. The file and raw-YAML tools refuse to write while this is off. | `true` |
| `auto_backup_throttle_minutes` | At most one auto-backup snapshot per entity per this many minutes (range 0-1440). `0` captures on every write. | `0` |
| `auto_backup_retain_per_entity` | The most snapshots kept per entity (range 1-10000). | `100` |
| `enable_snapshot_delete` | Lets `ha_manage_backup` delete full Home Assistant snapshots. Scheduled, newest and too-young snapshots stay protected. | `false` |
| `snapshot_delete_min_age_days` | How old a snapshot must be, in days, before it can be deleted (range 0-365). `0` turns off the age limit. | `7` |
| `tool_search_max_results` | Max hidden tools returned per `ha_search_tools` call (range 2-10); a pinned tool that ranks inside that top count is added as a name-only stub on top of it | `5` |
| `disabled_tools` | Comma-separated list of tool names to disable (seed value; web UI is primary). Mandatory tools can't be disabled and are unaffected. | empty |
| `pinned_tools` | Comma-separated list of tool names to pin when tool search is enabled (seed value; web UI is primary) | empty |
| `verify_ssl` | Verify the HA server's TLS certificate. Disable for self-signed certs or hostname mismatches. Weakens security — leave on unless needed. | `true` |

*Removed in 7.4.x:* `enable_skills` *and* `enable_skills_as_tools`*. Bundled skills are now always served via* `skill://` *resources (for resource-capable clients) and via the* `ha_get_skill_guide` *tool (for tool-only clients).*

Beta options are hidden under "Show unused optional configuration options" in the app Configuration tab. See [beta.md](https://github.com/homeassistant-ai/ha-mcp/blob/master/docs/beta.md) for details.

> ⚠️ **DANGER — beta toggles can permanently damage your Home Assistant installation.** They write to your YAML config, your filesystem, install custom components, and run arbitrary sandboxed Python. There is no warranty and no support guarantee — you enable these at your **own risk**. Take a Home Assistant backup before turning any of them on, and never enable in production without one.

### Permissions

Like the stable app, the dev app requests `hassio_role: manager` to
fetch app, system-service, and HA-core logs via the Supervisor REST API
(`/addons/<slug>/logs`, `/<service>/logs`, `/core/logs`) — `default` returns
403 on these endpoints (see #1116). The role also grants
start/stop/install/update on other apps, and ha-mcp does use that write
side: `ha_manage_app` installs, starts, stops, restarts and reconfigures
apps. Read-only mode blocks all of it except HTTP GET proxy reads of an
app's own API, so operators who want the role without the authority should
turn that mode on.

## Tool Settings Web UI

The app exposes a web-based settings page for managing which tools are available to AI assistants. Click **"Open Web UI"** on the app info page to access it.

Features:
- **Enable/disable individual tools** — toggle each tool on or off
- **Pin tools** — keep tools always visible when `enable_tool_search` is on
- **Per-group master toggle** — enable/disable all tools in a group (HACS, System, etc.) with one click
- **Search** — filter tools by name or title
- **Mandatory tools** — `ha_search`, `ha_get_overview`, `ha_get_state`, `ha_report_issue`, `ha_manage_backup` are always enabled and cannot be disabled (listing one in `disabled_tools` is a silent no-op — it keeps running). `ha_get_skill_guide` is additionally locked enabled while strict best-practices mode (`enable_strict_mandatory_bps`) is on — strict mode publishes its acknowledgment key only through that tool; turn strict mode off first to disable it
- **Feature-gated tools** — `ha_config_set_yaml` (requires `enable_yaml_config_editing`) and filesystem tools (require `enable_filesystem_tools`) appear in the list with a note if their feature flag is off
- **In-UI restart** — a "Restart App" button appears after saving to apply changes with one click

**Important:** Tool configuration changes require an app restart to take effect. The UI will prompt you to restart after saving.

### Non-add-on installations

In Docker (`ha-mcp-web`) and standalone HTTP installations, the settings UI is mounted under your MCP secret path. Open `http://<host>:<port>/<secret_path>/settings` (the same URL prefix that protects your MCP endpoint). This keeps the auth posture consistent — anyone who can reach your MCP endpoint can also use the settings UI; anyone who can't, can't.

### Text-field fallback

If you prefer not to use the web UI (or want to set these before first start), the `disabled_tools` and `pinned_tools` options accept comma-separated tool names as seed values. On first start, the app creates `/data/tool_config.json` from these values. After that, the web UI is the source of truth.

## Updates

The dev channel updates automatically with every commit to master. You may receive multiple updates per day.

To check for updates:
1. Go to Settings > Apps (Settings > Add-ons before Home Assistant 2026.2)
2. Click on "Home Assistant MCP Server (Dev)"
3. Click "Check for updates"

## Switching to Stable

If you want to switch back to stable releases:
1. Uninstall this dev app
2. Install the main "Home Assistant MCP Server" app

Your configuration will need to be reconfigured.

## Reporting Issues

When reporting issues from the dev channel, please include:
- The commit SHA (shown in the app info)
- Steps to reproduce
- Any error logs from the app

Issues: https://github.com/homeassistant-ai/ha-mcp/issues
