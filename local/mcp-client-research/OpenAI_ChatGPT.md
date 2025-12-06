# ChatGPT (OpenAI)

## Overview
ChatGPT supports MCP servers via the Connectors feature (requires Developer Mode).

---

## Source: awesome-remote-mcp-servers

### Connector Setup
1. Navigate to Settings → Connectors → Advanced Settings
2. Enable Developer Mode
3. Create new connector:
   - Name: `server-name`
   - URL: `https://your-server.com/sse`
   - Authentication: OAuth (if required)

---

## Source: MicrosoftDocs/mcp

### Manual Setup
1. Go to Settings → Connectors
2. Click Advanced settings
3. Enable Developer mode (toggle ON)
4. Create custom connector with:
   - Name
   - URL: `https://your-server.com/api/mcp`
   - Authentication: None (or as required)

---

## Notes
- Requires Developer Mode to be enabled
- Web-based configuration only (no config file)
- Supports HTTP/SSE transport
- Authentication options: None, OAuth
- Limited to remote servers (no local/stdio support)
- May require ChatGPT Plus subscription
