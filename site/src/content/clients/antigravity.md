---
name: Antigravity
company: Google
logo: /logos/google.svg
transports: ['stdio']
configFormat: json
configLocation: mcp_config.json (in Antigravity UI)
accuracy: 3
order: 15
---

## Configuration

Google Antigravity supports MCP servers via the built-in MCP Store and custom configuration.

> **Recommended:** Use stdio mode for reliable connectivity. HTTP mode may experience connection timeout issues.

### Accessing MCP Config

1. In Antigravity, click the **...** menu in the Agent pane
2. Select **MCP Servers** to open the MCP Store
3. Click **Manage MCP Servers** at the top
4. Click **View raw config** to edit `mcp_config.json`

### stdio Configuration (Recommended)

```json
{
  "mcpServers": {
    "home-assistant": {
      "command": "uvx",
      "args": ["ha-mcp@latest"],
      "env": {
        "HOMEASSISTANT_URL": "{{HOMEASSISTANT_URL}}",
        "HOMEASSISTANT_TOKEN": "{{HOMEASSISTANT_TOKEN}}",
        "FASTMCP_SHOW_CLI_BANNER": "false"
      }
    }
  }
}
```

> **Note:** `FASTMCP_SHOW_CLI_BANNER=false` disables the startup banner, which prevents "Unexpected server output" errors in Antigravity.

**Important:** Use absolute paths if specifying a local command. Restart the Agent session after saving.

### HTTP Configuration (Experimental)

> ⚠️ HTTP mode may experience "connection closed" or "SSE stream failed to reconnect" errors. Use stdio if possible.

```json
{
  "mcpServers": {
    "home-assistant": {
      "serverUrl": "{{MCP_SERVER_URL}}"
    }
  }
}
```

## Troubleshooting

- **"Unexpected server output" error:** Add `"FASTMCP_SHOW_CLI_BANNER": "false"` to your env config (see example above)
- **Tools load but fail when called:** Try stdio mode instead of HTTP
- **"EOF" errors:** Ensure command paths are absolute, not relative
- **First run timeout:** Run `uvx ha-mcp@latest --version` in terminal first to cache the package
- **Connection issues:** Restart the Agent session after config changes

## Notes

- Web-based configuration (edit JSON in browser)
- stdio is more reliable than HTTP for this MCP server
- See [Antigravity MCP Guide](https://antigravity.codes/blog/antigravity-mcp-tutorial) for general MCP setup
- Known issues: [#318](https://github.com/homeassistant-ai/ha-mcp/issues/318)
