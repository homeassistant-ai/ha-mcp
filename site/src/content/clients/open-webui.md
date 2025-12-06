---
name: Open WebUI
company: Open WebUI
logo: /logos/open-webui.svg
transports: ['streamable-http']
configFormat: ui
accuracy: 4
order: 14
httpNote: Requires Streamable HTTP - local or remote server
---

## Configuration

Open WebUI natively supports MCP servers via Streamable HTTP transport.

**Requirements:**
- Open WebUI v0.6.31+
- MCP server running in HTTP mode

### Setup Steps

1. Navigate to **Admin Settings** → **External Tools**
2. Click **+ (Add Server)**
3. Set **Type** to **"MCP (Streamable HTTP)"**
4. Enter:
   - **Server URL:** `{{MCP_SERVER_URL}}`
   - **Auth:** Configure if using authentication
5. Click **Save**
6. Restart Open WebUI if prompted

### Local Network Example

If ha-mcp is running on your network:

```
Server URL: http://192.168.1.100:8086/mcp
```

### Remote Example (HTTPS)

If using a secure tunnel:

```
Server URL: https://your-tunnel.trycloudflare.com/secret_abc123
```

## Supported Transports

- **Streamable HTTP** - Native support (recommended)

## Notes

- Web-based configuration only (no config file)
- Supports both HTTP (local) and HTTPS (remote) URLs
- Multi-tenant environment with per-user authentication
- Use [mcpo](https://github.com/open-webui/mcpo) proxy for stdio-based MCP servers
- See [Open WebUI MCP docs](https://docs.openwebui.com/features/mcp/) for details
