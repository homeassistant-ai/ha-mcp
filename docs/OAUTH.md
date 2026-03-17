# OAuth Authentication for ha-mcp (Beta)

> **Status:** Beta - OAuth provides an alternative to the private URL method. It's fully functional but still being refined.

> **Breaking change:** `HOMEASSISTANT_URL` is now a required environment variable in OAuth mode. The consent form no longer accepts a Home Assistant URL for security reasons.

OAuth authentication allows multiple users to authenticate with their own Home Assistant Long-Lived Access Token via a consent form.

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

### 1. Expose with HTTPS

```bash
# Quick tunnel for testing
cloudflared tunnel --url http://localhost:8086
```

For production, set up a [persistent Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/).

### 2. Start OAuth Server

**Docker:**
```bash
docker run -d --name ha-mcp-oauth \
  -p 8086:8086 \
  -e HOMEASSISTANT_URL=http://homeassistant.local:8123 \
  -e MCP_BASE_URL=https://your-tunnel.trycloudflare.com \
  ghcr.io/homeassistant-ai/ha-mcp:latest \
  ha-mcp-oauth
```

**uvx:**
```bash
export HOMEASSISTANT_URL=http://homeassistant.local:8123
export MCP_BASE_URL=https://your-tunnel.trycloudflare.com
uvx --from=ha-mcp@latest ha-mcp-oauth
```

### 3. Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `HOMEASSISTANT_URL` | **Required.** URL of the Home Assistant instance | None |
| `MCP_BASE_URL` | **Required.** Public URL where this server is accessible | None |
| `MCP_PORT` | Server port | `8086` |
| `MCP_SECRET_PATH` | MCP endpoint path | `/mcp` |
| `HOMEASSISTANT_TOKEN` | Long-Lived Access Token to connect ha-mcp to your HA ([how to generate](https://www.home-assistant.io/docs/authentication/#your-account-profile)) | None |
| `OAUTH_ENCRYPTION_KEY` | Persistent token encryption, see below | None |

> **Note:** As of v7.0.0, both `HOMEASSISTANT_URL` and `HOMEASSISTANT_TOKEN` are
> required server-side environment variables. The consent form no longer accepts
> user-supplied credentials (see [#749](https://github.com/homeassistant-ai/ha-mcp/issues/749)).

### Persistent token encryption

Set `OAUTH_ENCRYPTION_KEY` to keep OAuth tokens valid across server restarts.
Without it, a temporary key is generated on each start and all tokens are
invalidated when the container restarts.

Generate a stable key once and store it securely:
```bash
openssl rand -hex 32
```

Pass it as an environment variable:
```bash
-e OAUTH_ENCRYPTION_KEY=your_generated_key_here
```

### 4. Connect in Claude.ai

1. Go to **Settings** → **Connectors** → **Add custom connector**
2. Enter URL: `https://your-tunnel.com/mcp`
3. Click **Add**
4. In the consent form that opens:
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

The `/mcp` path is required - this is where the MCP server endpoints are mounted.

### "Invalid credentials" after authorizing

Verify your Long-Lived Access Token:
- Generate a fresh token in HA: Profile → Security → Long-lived access tokens
- Copy the complete token

Check that `HOMEASSISTANT_URL` is correct and accessible from the server running ha-mcp.

### Do tokens persist across server restarts?

**Yes!** Access tokens are stateless and self-contained - they work across server restarts and multi-instance deployments without any configuration.

### Can I use OAuth with Home Assistant OS?

No. The ha-mcp add-on doesn't support OAuth mode.

**Alternatives:**
- Run ha-mcp OAuth on another device (Raspberry Pi, NAS, PC)
- Deploy to a cloud server (AWS, DigitalOcean, etc.)
- Use Home Assistant Container instead of HAOS

The OAuth server needs network access to your Home Assistant instance.

---

**Back to:** [Main Documentation](../README.md)
