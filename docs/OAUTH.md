# OAuth Authentication for ha-mcp (Beta)

> **Status:** Beta — OAuth provides an alternative to the private URL method. Fully functional but still being refined.

OAuth authentication lets multiple users authenticate with their own Home Assistant Long-Lived Access Token via a consent form.

## Migrating from v6.x

v7.0.0 removed the Home Assistant URL field from the OAuth consent form. You must now set `HOMEASSISTANT_URL` as a server-side environment variable.

**Why:** The consent form had two security vulnerabilities:
- **SSRF** ([GHSA-fmfg-9g7c-3vq7](https://github.com/homeassistant-ai/ha-mcp/security/advisories/GHSA-fmfg-9g7c-3vq7)): The URL field let an attacker submit arbitrary URLs to probe internal networks through the ha-mcp server.
- **XSS** ([GHSA-pf93-j98v-25pv](https://github.com/homeassistant-ai/ha-mcp/security/advisories/GHSA-pf93-j98v-25pv)): Unescaped HTML in the consent form allowed cross-site scripting.

Removing the URL field and sanitizing form output eliminates both attack surfaces.

**What to do:** Add `HOMEASSISTANT_URL` to your server environment before starting ha-mcp:

**Docker:**
```bash
docker run -d -p 8086:8086 \
  -e HOMEASSISTANT_URL=https://your-ha-instance.example.com \
  -e MCP_BASE_URL=https://your-mcp-server.example.com \
  ghcr.io/homeassistant-ai/ha-mcp:latest ha-mcp-oauth
```

**uvx:**
```bash
HOMEASSISTANT_URL=https://your-ha-instance.example.com \
MCP_BASE_URL=https://your-mcp-server.example.com \
uvx --from=ha-mcp@latest ha-mcp-oauth
```

The consent form now accepts only the Long-Lived Access Token. Everything else stays the same.

---

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

> **Note:** `HOMEASSISTANT_TOKEN` is NOT required in OAuth mode. Each user provides their own Long-Lived Access Token via the consent form.

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
