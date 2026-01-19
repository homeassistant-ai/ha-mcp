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

## Overview

Claude.ai (web interface) supports MCP servers via the Connectors feature. **ha-mcp supports OAuth 2.1 authentication**, providing a seamless zero-config experience for Claude.ai users.

**Requirements:**
- HTTPS URL (HTTP not supported)
- Claude Pro, Max, Team, or Enterprise subscription
- Remote server with secure tunnel

## Authentication Methods

| Method | Best For | Credentials Stored |
|--------|----------|-------------------|
| **OAuth 2.1** (Recommended) | Multiple users, secure access | Per-user, via consent form |
| **Pre-configured Token** | Single user, simpler setup | In server environment |

---

## Option 1: OAuth Mode (Recommended)

OAuth mode provides **secure, zero-config authentication**. Users enter their Home Assistant credentials via a consent form when connecting.

### How OAuth Works

1. You add ha-mcp as a connector in Claude.ai
2. Claude.ai redirects to the ha-mcp consent form
3. You enter your Home Assistant URL and Long-Lived Access Token
4. Credentials are validated against your HA instance
5. You're connected! Credentials are encrypted in your session token

### Server Setup

**Using Docker:**

```bash
docker run -d --name ha-mcp-oauth \
  -p 8086:8086 \
  -e MCP_BASE_URL=https://your-public-url.com \
  -e OAUTH_ENCRYPTION_KEY=your-32-byte-base64-key \
  ghcr.io/homeassistant-ai/ha-mcp:latest \
  ha-mcp-oauth
```

**Using uvx:**

```bash
MCP_BASE_URL=https://your-public-url.com \
OAUTH_ENCRYPTION_KEY=your-32-byte-base64-key \
uvx ha-mcp@latest ha-mcp-oauth
```

### Environment Variables for OAuth

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `MCP_BASE_URL` | Public HTTPS URL of your server | `http://localhost:8086` | **Yes** (for production) |
| `MCP_PORT` | Server port | `8086` | No |
| `MCP_SECRET_PATH` | MCP endpoint path | `/mcp` | No |
| `OAUTH_ENCRYPTION_KEY` | 32-byte base64 key for token encryption | Auto-generated | **Recommended** |
| `LOG_LEVEL` | Logging verbosity | `INFO` | No |

> **Note:** If `OAUTH_ENCRYPTION_KEY` is not set, a temporary key is generated. Tokens will be invalidated on server restart. For production, generate a persistent key:
> ```bash
> python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
> ```

### Expose Server with HTTPS

Claude.ai requires HTTPS. Use a secure tunnel:

**Quick Tunnel (Testing):**
```bash
cloudflared tunnel --url http://localhost:8086
# Gives you: https://random-words.trycloudflare.com
# MCP URL: https://random-words.trycloudflare.com/mcp
```

**Persistent Tunnel:** See [Cloudflare Tunnel documentation](/setup?connection=remote&deployment=cloudflared)

### Connect in Claude.ai

1. Open [Claude.ai](https://claude.ai)
2. Go to **Settings** → **Connectors**
3. Click **Add custom connector**
4. Enter:
   - **Name:** Home Assistant
   - **URL:** `https://your-public-url.com/mcp`
5. Click **Add**
6. You'll be redirected to the ha-mcp consent form
7. Enter your Home Assistant URL (e.g., `http://homeassistant.local:8123`)
8. Enter your Long-Lived Access Token ([How to get a token](/faq#token-invalid-or-authentication-errors))
9. Click **Authorize**
10. You're connected!

---

## Option 2: Pre-configured Token Mode

For single-user setups where you want to skip the OAuth consent form.

### Server Setup

```bash
docker run -d --name ha-mcp \
  -p 8086:8086 \
  -e HOMEASSISTANT_URL=http://homeassistant.local:8123 \
  -e HOMEASSISTANT_TOKEN=your_long_lived_token \
  ghcr.io/homeassistant-ai/ha-mcp:latest \
  ha-mcp-web
```

### Connect in Claude.ai

1. Expose with HTTPS (see above)
2. Add connector with URL: `https://your-public-url.com/mcp`
3. No OAuth flow - connects directly using pre-configured credentials

---

## Supported Transports

- **Streamable HTTP** - Supported (recommended, used by OAuth mode)
- **SSE (Server-Sent Events)** - Supported

## Notes

- Web-based configuration only (no config file)
- Requires HTTPS endpoint (remote deployment required)
- Remote MCP Connectors are currently in beta
- Use the "Search and tools" button in chat to enable/disable specific tools
- OAuth tokens expire after 1 hour (auto-refresh supported)
- Refresh tokens valid for 7 days
