---
name: Antigravity
company: Google
logo: /logos/google.svg
transports: ['streamable-http']
configFormat: ui
accuracy: 4
order: 15
httpNote: Requires Streamable HTTP - remote server with HTTPS
---

## Configuration

Google Antigravity supports MCP servers via the built-in MCP Store and custom configuration.

**Requirements:**
- Antigravity account
- MCP server running in HTTP mode with HTTPS

### Setup Steps

1. In Antigravity, click the **...** menu in the Agent pane
2. Select **MCP Servers** to open the MCP Store
3. Click **Manage MCP Servers** at the top
4. Click **View raw config** to edit `mcp_config.json`
5. Add the Home Assistant MCP configuration:

```json
{
  "mcpServers": {
    "home-assistant": {
      "serverUrl": "{{MCP_SERVER_URL}}"
    }
  }
}
```

6. Save and restart the Agent session

## Notes

- Web-based configuration (edit JSON in browser)
- Requires HTTPS URL (use Cloudflare Tunnel or similar)
- Uses `serverUrl` key (not `url`)
- See [Antigravity MCP Guide](https://antigravity.codes/blog/antigravity-mcp-tutorial) for details
