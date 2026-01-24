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

**1. Deploy Server with HTTPS**

Claude.ai requires HTTPS. Deploy ha-mcp with a secure tunnel:

**Quick Tunnel (Testing):**
```bash
# Start ha-mcp server
docker run -d --name ha-mcp \
  -p 8086:8086 \
  -e HOMEASSISTANT_URL=http://homeassistant.local:8123 \
  -e HOMEASSISTANT_TOKEN=your_long_lived_token \
  -e MCP_SECRET_PATH=/__my-secret-path-$(uuidgen)__ \
  ghcr.io/homeassistant-ai/ha-mcp:latest \
  ha-mcp-web

# Create tunnel
cloudflared tunnel --url http://localhost:8086
# Gives you: https://random-words.trycloudflare.com
```

**Persistent Tunnel:** See [Cloudflare Tunnel documentation](/setup?connection=remote&deployment=cloudflared)

> **Security Note:** Use a unique, hard-to-guess `MCP_SECRET_PATH` to prevent unauthorized access. Without OAuth, the secret path is your only protection.

**2. Connect in Claude.ai**

1. Open [Claude.ai](https://claude.ai)
2. Go to **Settings** → **Connectors**
3. Click **Add custom connector**
4. Enter:
   - **Name:** Home Assistant
   - **URL:** `https://your-tunnel.com/__your-secret-path__` (include your secret path)
5. Click **Add**
6. You're connected!

## Environment Variables

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `HOMEASSISTANT_URL` | Your Home Assistant URL | - | Yes |
| `HOMEASSISTANT_TOKEN` | Long-lived access token | - | Yes |
| `MCP_SECRET_PATH` | Secret path for security | `/mcp` | **Recommended** (use unique value) |
| `MCP_PORT` | Server port | `8086` | No |

## Supported Transports

- **Streamable HTTP** - Supported (recommended)
- **SSE (Server-Sent Events)** - Supported

## Notes

- Web-based configuration only (no config file)
- Requires HTTPS endpoint (remote deployment required)
- Remote MCP Connectors are currently in beta
- Use the "Search and tools" button in chat to enable/disable specific tools
- Restart both Claude.ai and the MCP server if you encounter issues

---

## 🔐 Alternative: OAuth Authentication (Beta)

Looking for a more secure authentication method that doesn't rely on secret URLs?

**OAuth mode** provides:
- No need for secret paths - real authentication instead of obscurity
- Multi-user support with per-user credentials
- Secure consent form for entering Home Assistant credentials

[**See OAuth Setup Guide →**](https://github.com/homeassistant-ai/ha-mcp/blob/master/docs/OAUTH.md)

> **Note:** OAuth mode is currently in beta. The private URL method above is the stable, recommended approach.
