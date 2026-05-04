---
name: OpenCode
company: Anomaly
logo: /logos/opencode.svg
transports: ['stdio', 'streamable-http']
configFormat: json
configLocation: ~/.config/opencode/opencode.json (global) or opencode.json (project)
accuracy: 5
order: 18
---

## Configuration

OpenCode supports MCP servers through the `mcp` key in its JSON (or JSONC) configuration. Both stdio (`type: "local"`) and HTTP streaming (`type: "remote"`) are first-class — no proxy shim is required.

### Config File Locations

- **Global:** `~/.config/opencode/opencode.json`
- **Project:** `opencode.json` (or `opencode.jsonc`) in the project root — overrides global
- **Custom path:** `OPENCODE_CONFIG=/path/to/file.json`

OpenCode merges configs from all locations (project overrides global). See [config precedence](https://opencode.ai/docs/config/#precedence-order).

### stdio Configuration (Local)

<!-- The uvx-stdio shape below (type, command, enabled, environment) is mirrored in -->
<!-- site/src/pages/setup.astro (wizard `isOpenCode` branch) — keep aligned. -->
<!-- Note: the Docker variant in setup.astro intentionally omits the top-level -->
<!-- `environment` key (env vars are inlined as `-e KEY=VAL` in the command array). -->

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "home-assistant": {
      "type": "local",
      "command": ["uvx", "ha-mcp@latest"],
      "enabled": true,
      "environment": {
        "HOMEASSISTANT_URL": "{{HOMEASSISTANT_URL}}",
        "HOMEASSISTANT_TOKEN": "{{HOMEASSISTANT_TOKEN}}"
      }
    }
  }
}
```

> **Note:** OpenCode's `command` is a single array containing both the executable and its arguments (no separate `args` field), and the env block is named `environment` (not `env`).

### Streamable HTTP Configuration (Network/Remote)

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "home-assistant": {
      "type": "remote",
      "url": "{{MCP_SERVER_URL}}",
      "enabled": true
    }
  }
}
```

### With Custom Headers (e.g., Bearer Auth)

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "home-assistant": {
      "type": "remote",
      "url": "{{MCP_SERVER_URL}}",
      "headers": {
        "Authorization": "Bearer {env:HA_MCP_TOKEN}"
      },
      "oauth": false
    }
  }
}
```

OpenCode supports `{env:VAR}` substitution inside the config — handy for keeping secrets out of the file.

### OAuth (Automatic)

If your MCP server speaks OAuth, OpenCode handles Dynamic Client Registration automatically. No extra config is needed; OpenCode prompts on first use, or you can trigger it manually:

```bash
opencode mcp auth home-assistant
```

## Quick Setup with CLI Commands

OpenCode provides an interactive `opencode mcp add` command. Unlike Codex/Gemini CLI, it does not accept inline flags — it walks you through the same fields you would put in `opencode.json`.

```bash
opencode mcp add
```

When prompted, enter:

| Field | Value |
|-------|-------|
| Server name | `home-assistant` |
| Type | `local` |
| Command | `uvx ha-mcp@latest` |
| Environment variables | `HOMEASSISTANT_URL=...`, `HOMEASSISTANT_TOKEN=...` |

For HTTP/remote setups, choose `remote` for the type and paste your `{{MCP_SERVER_URL}}` when asked for the URL.

If you prefer a copy-paste artifact, use the JSON config blocks above instead — `opencode mcp add` only writes the same shape into `opencode.json`.

## Management Commands

```bash
# List configured servers and their auth status
opencode mcp list

# Trigger or refresh authentication
opencode mcp auth home-assistant

# Remove stored credentials
opencode mcp logout home-assistant

# Diagnose connection / OAuth issues
opencode mcp debug home-assistant
```

## Notes

- Config key is `mcp` (not `mcpServers`).
- Local servers use `type: "local"` with a single `command` array; `args` is not used.
- Environment variables go under `environment` (not `env`).
- Remote servers use `type: "remote"` with `url`; both stdio and streamable HTTP are supported natively, so no `mcp-proxy` is required for HTTP setups.
- Tools from this server are namespaced as `home-assistant_*`. To restrict a server to specific agents, disable the namespace globally and re-enable it per agent — see [MCP per-agent docs](https://opencode.ai/docs/mcp-servers/#per-agent).
- Each MCP server adds to the prompt context. With ha-mcp's 92+ tools this is non-trivial; consider gating it to a dedicated agent if you primarily use OpenCode for non-HA work.
