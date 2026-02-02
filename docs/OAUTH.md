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
  ghcr.io/homeassistant-ai/ha-mcp:latest \
  ha-mcp-oauth
```

**uvx:**
```bash
uvx ha-mcp@latest ha-mcp-oauth
```

### 2. Environment Variables (All Optional!)

| Variable | Description | Default |
|----------|-------------|---------|
| `MCP_PORT` | Server port | `8086` |
| `MCP_BASE_URL` | Public URL (auto-detected if not set) | Detected from request headers |

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

Make sure you're using the correct URL in Claude.ai:

```
✅ Correct: https://your-tunnel.com/mcp
❌ Wrong:   https://your-tunnel.com
```

The server auto-detects its base URL from incoming requests, so you don't need to configure anything - just use `your-tunnel/mcp` in Claude.ai.

### "Invalid credentials" on consent form

Check your Home Assistant URL format:
- Include protocol: `http://` or `https://`
- Include port if not default: `:8123`
- No trailing slash
- Example: `http://homeassistant.local:8123`

Verify your Long-Lived Access Token:
- Generate fresh token in HA: Profile → Security → Long-lived access tokens
- Copy the complete token

### Do tokens persist across server restarts?

**Yes!** The encryption key is automatically saved to `~/.ha-mcp/oauth_key` and reused on restart.

**For multi-instance deployments**, copy the key file to other servers:
```bash
scp ~/.ha-mcp/oauth_key server2:~/.ha-mcp/
```

Or use the `OAUTH_ENCRYPTION_KEY` environment variable to share the same key across all instances.

### Can I use OAuth with Home Assistant OS?

No. The ha-mcp add-on doesn't support OAuth mode.

**Alternatives:**
- Run ha-mcp OAuth on another device (Raspberry Pi, NAS, PC)
- Deploy to a cloud server (AWS, DigitalOcean, etc.)
- Use Home Assistant Container instead of HAOS

The OAuth server needs network access to your Home Assistant instance.

---

**Back to:** [Main Documentation](../README.md)
