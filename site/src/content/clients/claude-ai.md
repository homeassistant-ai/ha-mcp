---
name: Claude.ai
company: Anthropic
logo: /logos/anthropic.svg
transports: ['sse', 'streamable-http']
configFormat: ui
accuracy: 4
order: 8
httpNote: Requires HTTPS - Remote deployment required
---

## Configuration

Claude.ai (web interface) supports MCP servers via the Connectors feature.

**Requirements:**
- HTTPS URL (HTTP not supported)
- Claude Pro, Max, Team, or Enterprise subscription
- Remote server with secure tunnel

### Setup Steps

1. Open [Claude.ai](https://claude.ai)
2. Go to **Settings** → **Connectors**
3. Click **Add custom connector**
4. Enter:
   - **Name:** Home Assistant
   - **URL:** `{{MCP_SERVER_URL}}` (must be HTTPS)
5. Click **Add**
6. Authenticate if OAuth is required

### Important

Claude.ai **only supports HTTPS** - you cannot use HTTP URLs. You'll need to set up a secure tunnel (see Remote deployment options).

## Supported Transports

- **SSE (Server-Sent Events)** - Supported
- **Streamable HTTP** - Supported (recommended)

## Notes

- Web-based configuration only (no config file)
- Requires HTTPS endpoint (Remote deployment required)
- Remote MCP Connectors are currently in beta
- Use the "Search and tools" button in chat to enable/disable specific tools
