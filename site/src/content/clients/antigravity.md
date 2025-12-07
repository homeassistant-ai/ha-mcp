---
name: Antigravity
company: Google
logo: /logos/google.svg
transports: ['stdio', 'streamable-http']
configFormat: json
configLocation: mcp_config.json (in Antigravity UI)
accuracy: 4
order: 15
---

## Configuration

Google Antigravity supports MCP servers via the built-in MCP Store and custom configuration.

### Accessing MCP Config

1. In Antigravity, click the **...** menu in the Agent pane
2. Select **MCP Servers** to open the MCP Store
3. Click **Manage MCP Servers** at the top
4. Click **View raw config** to edit `mcp_config.json`

### stdio Configuration (Local)

```json
{
  "mcpServers": {
    "home-assistant": {
      "command": "uvx",
      "args": ["ha-mcp@latest"],
      "env": {
        "HOMEASSISTANT_URL": "{{HOMEASSISTANT_URL}}",
        "HOMEASSISTANT_TOKEN": "{{HOMEASSISTANT_TOKEN}}"
      }
    }
  }
}
```

### HTTP Configuration (Network/Remote)

```json
{
  "mcpServers": {
    "home-assistant": {
      "serverUrl": "{{MCP_SERVER_URL}}"
    }
  }
}
```

Save and restart the Agent session after making changes.

## Notes

- Web-based configuration (edit JSON in browser)
- Uses `serverUrl` key for HTTP (not `url`)
- Restart Agent session after config changes
- See [Antigravity MCP Guide](https://antigravity.codes/blog/antigravity-mcp-tutorial) for details
