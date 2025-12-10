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

1. Navigate to **Admin Panel** → **Settings** → **Tools**
2. Click **Manage Tool Servers**
   - Note: There's also "External Tools" in user settings — use the **Admin** one
3. Click **+ (Add Server)**
4. Enter:
   - **Server URL:** `{{MCP_SERVER_URL}}`
   - **Auth:** Select "None" (or configure if using authentication)
   - **ID** and **Name:** Fill in as desired
5. Click **Save**

### Finding Your MCP URL

**Home Assistant Add-on:** Check the add-on logs for the URL (e.g., `http://homeassistant.local:8086/private_xyz`)

**Docker on same host:** Use `http://host.docker.internal:8086/mcp`

**Local network:** Use `http://192.168.1.100:8086/mcp`

**Remote (HTTPS):** Use `https://your-tunnel.trycloudflare.com/secret_abc123`

### Running Open WebUI

```bash
docker run -d \
  -p 3000:8080 \
  -v open-webui:/app/backend/data \
  --name open-webui \
  ghcr.io/open-webui/open-webui:main
```

Access at: `http://localhost:3000`

## Supported Transports

- **Streamable HTTP** - Native support (recommended)

## Notes

- Web-based configuration only (no config file)
- Supports both HTTP (local) and HTTPS (remote) URLs
- Your LLM must support tool use (Ollama, OpenAI, Anthropic, etc.)
- Use [mcpo](https://github.com/open-webui/mcpo) proxy for stdio-based MCP servers
- See [Open WebUI MCP docs](https://docs.openwebui.com/features/mcp/) for details
