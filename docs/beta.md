# Beta Features

Some ha-mcp tools are gated behind feature flags and available only in the **dev channel** add-on (or via environment variables for non-add-on installs). Beta tools are still being evaluated and may change, be promoted to stable, or be removed based on field experience.

## Current beta tools

| Tool | Toggle / env var | Description |
|---|---|---|
| `ha_config_set_yaml` | `enable_yaml_config_editing` (dev add-on) / `ENABLE_YAML_CONFIG_EDITING=true` (env var) | Raw YAML editing of `configuration.yaml` and packages/*.yaml for YAML-only integrations. |
| `ha_list_files` | `enable_filesystem_tools` (dev add-on) / `HAMCP_ENABLE_FILESYSTEM_TOOLS=true` (env var) | List files in allowed directories (www/, themes/, custom_templates/). Requires `ha_mcp_tools` custom component. |
| `ha_read_file` | `enable_filesystem_tools` (dev add-on) / `HAMCP_ENABLE_FILESYSTEM_TOOLS=true` (env var) | Read files from allowed paths. Requires `ha_mcp_tools` custom component. |
| `ha_write_file` | `enable_filesystem_tools` (dev add-on) / `HAMCP_ENABLE_FILESYSTEM_TOOLS=true` (env var) | Write files to allowed directories. Requires `ha_mcp_tools` custom component. |
| `ha_delete_file` | `enable_filesystem_tools` (dev add-on) / `HAMCP_ENABLE_FILESYSTEM_TOOLS=true` (env var) | Delete files from allowed directories. Requires `ha_mcp_tools` custom component. |
| `ha_install_mcp_tools` | `enable_custom_component_integration` (dev add-on) / `HAMCP_ENABLE_CUSTOM_COMPONENT_INTEGRATION=true` (env var) | Installs the `ha_mcp_tools` custom component via HACS. |
| `ha_manage_custom_tool` | `enable_code_mode` (dev add-on) / `ENABLE_CODE_MODE=true` (env var) | Sandboxed Python "escape hatch" that lets AI assistants write, run, and save custom tools when no built-in tool covers the request. Code runs in pydantic-monty (no filesystem, no network); sandbox can call the HA REST API (`api_get`/`api_post`) or registered MCP tools (`call_tool`). |

## How to enable

### Option 1: Dev channel add-on (Home Assistant users)

1. Install the **Home Assistant MCP Server (Dev)** add-on. See [docs/dev-channel.md](dev-channel.md) for details.
2. Open the add-on's **Configuration** tab.
3. Enable "Show unused optional configuration options" to reveal beta toggles.
4. Enable the desired toggle (e.g., `enable_yaml_config_editing`, `enable_filesystem_tools`).
5. Restart the add-on.

`enable_yaml_config_editing`, `enable_filesystem_tools`, `enable_custom_component_integration`, and `enable_code_mode` are only available in the dev channel add-on. The stable add-on does not expose these beta toggles.

### Option 2: Environment variable (non-add-on installs)

```bash
export ENABLE_YAML_CONFIG_EDITING=true
uvx ha-mcp@latest
```

The tool registers only when its variable is `true`. Any other value (unset, `false`, `0`) leaves it disabled.

## Known limitations

### `ha_config_set_yaml`

This tool edits `configuration.yaml` and package files directly, bypassing Home Assistant's config-entry flow. It includes safeguards (backup before every edit, YAML validation, key allowlist, path traversal blocking, post-edit config check), but operators should be aware of the following:

**Config check has blind spots.** `ha_check_config` validates YAML syntax but does not catch all integration-level schema errors. An edit can pass validation, HA boots cleanly, but the target entity silently does not exist. Common LLM mistakes include mixing legacy and modern template sensor syntax, wrong field names (`value_template:` vs `state:`), and bad Jinja expressions.

**`action: remove` removes the entire top-level key.** Asking an LLM to remove a single sensor can result in the entire `template:` key being deleted, not just the intended entry.

**Most keys require a full HA restart.** Only `template`, `mqtt`, and `group` support reload. All other keys require restarting Home Assistant for changes to take effect. The tool response includes `post_action` indicating which is needed.

**`command_line:` entries execute shell commands.** The allowlist includes `command_line:` for legitimate use cases, but an LLM could inadvertently create a sensor with a command that reads sensitive files or modifies the system.

**Recovery requires filesystem access.** If an edit causes HA to enter recovery mode (e.g., a bad `!include` reference), `ha_config_set_yaml` cannot fix its own damage since the custom component doesn't load in recovery mode. Recovery requires SSH, the File Editor add-on, or `docker exec`.

**Backups are filesystem-only.** Per-edit backups are written to `www/yaml_backups/` but no ha-mcp tool can restore them. They are a safety net for manual recovery.

**Recommended prerequisites:**
- Comfort with editing `configuration.yaml` via SSH or File Editor when things go wrong
- Understanding that dedicated tools (`ha_config_set_helper`, `ha_config_set_automation`, `ha_config_set_script`, `ha_config_set_scene`, etc.) should be preferred for anything they support

### `ha_list_files`, `ha_read_file`, `ha_write_file`, `ha_delete_file`

These tools provide direct file access to your Home Assistant filesystem and require `HAMCP_ENABLE_FILESYSTEM_TOOLS=true` and the `ha_mcp_tools` custom component installed and active.

`HAMCP_ENABLE_CUSTOM_COMPONENT_INTEGRATION=true` is only needed if you want to allow the `ha_install_mcp_tools` installer tool; it is not required for the filesystem tools themselves.

**Access is restricted but sensitive.** Only `www/`, `themes/`, and `custom_templates/` are writable. `ha_read_file` additionally allows reading config YAML files, logs, and `custom_components/`. An AI assistant with these tools enabled has meaningful read access to your HA configuration.

**No undo.** `ha_delete_file` and `ha_write_file` (with `overwrite=True`) are irreversible. There is no recycle bin or automatic backup for file operations.

**Requires the custom component.** If `ha_mcp_tools` is not installed and active, all file tools will return an error with installation instructions.

### `ha_manage_custom_tool`

This tool exposes a sandboxed Python interpreter (`pydantic-monty`) to the AI as an escape hatch for operations no built-in tool covers. It also lets the AI save tools for reuse via `save_as` / `run_saved` / `list_saved`. Sandbox code can either hit the HA REST API directly (`api_get`/`api_post`) or call other registered MCP tools (`call_tool`). The sandbox blocks filesystem and network I/O, but operators should still be aware of the following:

**The AI gets to write and run code on your HA instance.** Even though the sandbox prevents it from touching the filesystem or the public network, code can still call any tool the MCP server has registered, including write/destructive tools, and can hit any endpoint reachable via the HA REST API. Treat this like giving the AI a generic "do whatever existing tools allow you to do, in any combination" capability — not a tightly scoped per-feature tool.

**Saved tools are stored in process memory only.** Tools you ask the AI to `save_as` live in the running ha-mcp process. They are lost on add-on / server restart. There is no on-disk persistence and no `ha_remove_saved_tool` — the only way to drop a saved tool is to restart.

**Recursive self-call is blocked, but composition is not.** The sandbox refuses to invoke `ha_manage_custom_tool` from inside itself, so it can't directly recurse, but it can chain together every other tool the server registers. A buggy or adversarial prompt can still cause unexpected fan-out across destructive tools.

**Resource limits are best-effort.** 30s wall-clock, 10 MB memory, recursion depth 100, and 100 API/tool calls per execution are enforced by the sandbox (configurable via `CODE_MODE_*` env vars). They protect against runaway loops, not against intentionally crafted abuse — keep `ENABLE_CODE_MODE=false` in any environment where untrusted prompts can reach the server.

**ARM platforms require the async sandbox path.** On systems where `Monty.run_async` is unavailable, the tool fails fast with a clear error rather than falling back silently.

**Recommended prerequisites:**
- You're comfortable with the AI authoring small Python snippets that wrap existing tools or HA REST endpoints
- You have `destructiveHint=True` confirmation enabled on the MCP client and you actually read the prompts
