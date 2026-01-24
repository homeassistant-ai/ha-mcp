# OAuth Authentication for ha-mcp (Beta)

> **Status:** Beta - OAuth provides an alternative to the private URL method. It's fully functional but still being refined.

OAuth authentication allows users to enter their Home Assistant credentials via a consent form instead of pre-configuring them on the server.

## When to Use OAuth

**Use OAuth if you want:**
- Real authentication instead of relying on secret URLs
- Multi-user support with per-user credentials
- Users to authenticate themselves via consent form

**Use private URL method if you want:**
- Simpler setup (recommended for most users)
- Single-user access

> **Note:** Both methods provide identical Home Assistant access. OAuth only changes how users authenticate.

---

## Setup

### 1. Start OAuth Server

**Docker:**
```bash
docker run -d --name ha-mcp-oauth \
  -p 8086:8086 \
  -e MCP_BASE_URL=https://your-tunnel.com \
  -e OAUTH_ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") \
  ghcr.io/homeassistant-ai/ha-mcp:latest \
  ha-mcp-oauth
```

**uvx:**
```bash
MCP_BASE_URL=https://your-tunnel.com \
OAUTH_ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") \
uvx ha-mcp@latest ha-mcp-oauth
```

### 2. Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `MCP_BASE_URL` | Your public domain (no path) | `https://your-tunnel.com` |
| `OAUTH_ENCRYPTION_KEY` | 32-byte key for token encryption | Generate with command above |
| `MCP_PORT` | Server port (optional) | `8086` (default) |

### 3. Expose with HTTPS

```bash
# Quick tunnel for testing
cloudflared tunnel --url http://localhost:8086
```

For production, set up a [persistent Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/).

### 4. Connect in Claude.ai

1. Go to **Settings** → **Connectors** → **Add custom connector**
2. Enter URL: `https://your-tunnel.com/mcp`
3. Click **Add**
4. In the consent form that opens:
   - Enter your Home Assistant URL (e.g., `http://homeassistant.local:8123`)
   - Enter your Long-Lived Access Token ([how to generate](https://www.home-assistant.io/docs/authentication/#your-account-profile))
5. Click **Authorize**

---

## FAQ

### "404 Not Found" when connecting

**Problem:** `MCP_BASE_URL` includes `/mcp` at the end.

```bash
# ❌ Wrong
MCP_BASE_URL=https://your-tunnel.com/mcp

# ✅ Correct
MCP_BASE_URL=https://your-tunnel.com
```

Then use `https://your-tunnel.com/mcp` in Claude.ai.

### "Invalid credentials" on consent form

Check your Home Assistant URL format:
- Include protocol: `http://` or `https://`
- Include port if not default: `:8123`
- No trailing slash
- Example: `http://homeassistant.local:8123`

Verify your Long-Lived Access Token:
- Generate fresh token in HA: Profile → Security → Long-lived access tokens
- Copy the complete token

### Session expires after server restart

Set a persistent `OAUTH_ENCRYPTION_KEY`. Without it, tokens are invalidated when the server restarts.

```bash
# Generate key
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Use it
docker run -e OAUTH_ENCRYPTION_KEY=your-key-here ...
```

### Can I use OAuth with Home Assistant OS?

No. The ha-mcp add-on doesn't support OAuth mode.

**Alternatives:**
- Run ha-mcp OAuth on another device (Raspberry Pi, NAS, PC)
- Deploy to a cloud server (AWS, DigitalOcean, etc.)
- Use Home Assistant Container instead of HAOS

The OAuth server needs network access to your Home Assistant instance.

---

**Back to:** [Main Documentation](../README.md)
