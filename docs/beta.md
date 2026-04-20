# Beta Features

Some ha-mcp tools are gated behind feature flags and available only in the **dev channel** add-on (or via environment variables for non-add-on installs). Beta tools are still being evaluated and may change, be promoted to stable, or be removed based on field experience.

## Current beta tools

| Tool | Toggle / env var | Description |
|---|---|---|
| `ha_config_set_yaml` | `enable_yaml_config_editing` (dev add-on) / `ENABLE_YAML_CONFIG_EDITING=true` (env var) | Raw YAML editing of `configuration.yaml` and packages/*.yaml for YAML-only integrations. |

## How to enable

### Option 1: Dev channel add-on (Home Assistant users)

1. Install the **Home Assistant MCP Server (Dev)** add-on. See [docs/dev-channel.md](dev-channel.md) for details.
2. Open the add-on's **Configuration** tab.
3. Enable the toggle (e.g., `enable_yaml_config_editing`).
4. Restart the add-on.

The stable add-on does not expose beta toggles.

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
