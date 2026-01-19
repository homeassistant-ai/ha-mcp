# FAQ & Troubleshooting

Common questions and solutions for ha-mcp setup.

## General Questions

### Do I need a Claude Pro subscription?

**No.** Claude Desktop works with a free Claude account. The MCP integration is available to all users, though free accounts have usage limits.

You can also use ha-mcp with other AI clients. See the [Setup Wizard](https://homeassistant-ai.github.io/ha-mcp/setup/) for 15+ supported clients.

### Do I need the Home Assistant Add-on?

**No.** The HA add-on is just one installation method. Most users run ha-mcp directly on their computer using `uvx` (recommended for Claude Desktop). The add-on is only needed if you want to run ha-mcp inside your Home Assistant OS environment.

### What's the difference between ha-mcp and Home Assistant's built-in MCP?

| Feature | Built-in HA MCP | ha-mcp |
|---------|-----------------|--------|
| Tools | ~15 basic tools | 80+ comprehensive tools |
| Focus | Device control | Full system administration |
| Automations | Limited | Create, edit, debug, trace |
| Dashboards | No | Full dashboard management |
| Cameras | No | Screenshot and analysis |

Built-in = operate devices. ha-mcp = administer your system.

### What's the difference between OAuth and token-based authentication?

| Feature | Token-Based | OAuth 2.1 |
|---------|-------------|-----------|
| **Setup** | Configure token in server environment | Users enter credentials via consent form |
| **Best for** | Single user, local clients (Claude Desktop) | Multi-user, remote clients (Claude.ai) |
| **Credentials stored** | In server config | Encrypted in user's session token |
| **Server command** | `ha-mcp` or `ha-mcp-web` | `ha-mcp-oauth` |
| **HTTPS required** | No (but recommended) | Yes (for production) |

**When to use OAuth:**
- Hosting ha-mcp for multiple users
- Using Claude.ai (web) as your client
- Exposing ha-mcp over the internet

**When to use token-based:**
- Single-user setup
- Local/LAN access only
- Claude Desktop or other local clients

---

## Try Without Your Own Home Assistant

Want to test before connecting to your own Home Assistant? Use our public demo:

| Setting | Value |
|---------|-------|
| **URL** | `https://ha-mcp-demo-server.qc-h.net` |
| **Token** | `demo` |
| **Web UI** | Login with `mcp` / `mcp` |

Just set `HOMEASSISTANT_TOKEN` to `demo` and ha-mcp will automatically use the demo credentials.

The demo environment resets weekly. Your changes won't persist.

---

## Troubleshooting

### SSL certificate errors (self-signed certificates)

If your Home Assistant uses HTTPS with a self-signed certificate or custom CA, you may see SSL verification errors.

**Docker solution:**

1. Create a combined CA bundle:
   ```bash
   cat $(python3 -m certifi) /path/to/your-ca.crt > combined-ca-bundle.crt
   ```

2. Mount it and set `SSL_CERT_FILE`:
   ```json
   {
     "mcpServers": {
       "home-assistant": {
         "command": "docker",
         "args": [
           "run", "--rm",
           "-e", "HOMEASSISTANT_URL=https://your-ha:8123",
           "-e", "HOMEASSISTANT_TOKEN=your_token",
           "-e", "SSL_CERT_FILE=/certs/ca-bundle.crt",
           "-v", "./combined-ca-bundle.crt:/certs/ca-bundle.crt:ro",
           "ghcr.io/homeassistant-ai/ha-mcp:latest"
         ]
       }
     }
   }
   ```

### "uvx not found" error

After installing uv, **restart your terminal** (or Claude Desktop) for the PATH changes to take effect.

**Mac:**
```bash
# Reload shell or restart terminal
source ~/.zshrc
# Or verify with full path
~/.local/bin/uvx --version
```

**Windows:**
```powershell
# Restart PowerShell/cmd after installing uv
# Or use full path
%USERPROFILE%\.local\bin\uvx.exe --version
```

### MCP server not showing in Claude Desktop

1. **Restart Claude completely** - Use Cmd+Q (Mac) or Alt+F4 (Windows), not just close the window
2. **Check config file location:**
   - Mac: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
3. **Verify JSON syntax** - No trailing commas, proper quotes
4. **Check the MCP icon** - Bottom left of Claude Desktop shows connected servers

### "Token invalid" or authentication errors

1. **Generate a new token:**
   - Home Assistant → Click your username (bottom left)
   - Security tab → Long-lived access tokens
   - Create Token → Copy immediately (shown only once)
2. **Check token format** - Don't wrap the token in quotes in your config
3. **Token expiration** - Tokens don't expire by default, but can be revoked

### Claude says it can't see Home Assistant

1. Open Claude Desktop **Settings** (gear icon)
2. Go to the **Developer** tab
3. Check **Local MCP Servers** for any errors
4. If "Home Assistant" is not listed, check your config file syntax
5. Try asking Claude: "Can you list your available tools?"

### Server works but responses are slow

1. **First request is slow** - `uvx` downloads packages on first run
2. **Subsequent requests** - Should be faster (packages cached)
3. **Alternative** - Use Docker for consistent performance

---

## OAuth Troubleshooting

### OAuth consent form not loading

1. **Check MCP_BASE_URL** - Must be set to your public HTTPS URL
2. **Verify HTTPS** - OAuth requires HTTPS in production
3. **Check tunnel** - Ensure Cloudflare Tunnel or similar is running

### "Invalid credentials" on consent form

1. **Verify Home Assistant URL:**
   - Include protocol: `http://` or `https://`
   - Include port if not default: `:8123`
   - Don't include trailing slash
   - Example: `http://homeassistant.local:8123`

2. **Verify Long-Lived Access Token:**
   - Generate a fresh token in HA → Profile → Security → Long-lived access tokens
   - Copy the full token (it's long!)
   - Token is validated against HA's `/api/config` endpoint

3. **Check network connectivity:**
   - The ha-mcp server must be able to reach your Home Assistant
   - If using Docker, ensure HA is accessible from the container

### OAuth session expired / logged out unexpectedly

1. **Check OAUTH_ENCRYPTION_KEY:**
   - If not set, tokens are invalidated on server restart
   - Generate a persistent key:
     ```bash
     python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
     ```
   - Add to your environment: `OAUTH_ENCRYPTION_KEY=your-generated-key`

2. **Token expiration times:**
   - Access tokens: 1 hour (auto-refresh)
   - Refresh tokens: 7 days
   - Re-authenticate if refresh token expires

### OAuth redirect loop or "invalid_grant" error

1. **Clear browser cookies** for your ha-mcp domain
2. **Check MCP_BASE_URL matches your actual URL** (including https://)
3. **Verify no URL path mismatch** between MCP_BASE_URL and tunnel config

### Claude.ai shows "401 Unauthorized"

1. **Ensure you're using OAuth mode:** Server should run `ha-mcp-oauth`
2. **Check the connector URL in Claude.ai** ends with `/mcp`
3. **Try removing and re-adding the connector**

---

## Configuration Options

### Environment Variables (Token Mode)

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `HOMEASSISTANT_URL` | Your Home Assistant URL | - | Yes |
| `HOMEASSISTANT_TOKEN` | Long-lived access token (or `demo` for demo env) | - | Yes |
| `BACKUP_HINT` | Backup recommendation level | `normal` | No |

### Environment Variables (OAuth Mode)

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `MCP_BASE_URL` | Public HTTPS URL of your server | `http://localhost:8086` | Yes (production) |
| `MCP_PORT` | Server port | `8086` | No |
| `MCP_SECRET_PATH` | MCP endpoint path | `/mcp` | No |
| `OAUTH_ENCRYPTION_KEY` | 32-byte base64 key for token encryption | Auto-generated | Recommended |
| `LOG_LEVEL` | Logging verbosity (DEBUG, INFO, WARNING, ERROR) | `INFO` | No |

> **Note:** In OAuth mode, `HOMEASSISTANT_URL` and `HOMEASSISTANT_TOKEN` are NOT required - users provide credentials via the consent form.

### Backup Hint Modes

| Mode | Behavior |
|------|----------|
| `strong` | Suggests backup before first modification each day/session |
| `normal` | Suggests backup only before irreversible operations (recommended) |
| `weak` | Rarely suggests backups |
| `auto` | Same as normal (future: auto-detection) |

---

## Feedback & Help

We'd love to hear how you're using ha-mcp!

- **[GitHub Discussions](https://github.com/homeassistant-ai/ha-mcp/discussions)** — Share how you use it, ask questions, show off your automations
- **[GitHub Issues](https://github.com/homeassistant-ai/ha-mcp/issues)** — Report bugs or request features
- **[Home Assistant Forum](https://community.home-assistant.io/t/brand-new-claude-ai-chatgpt-integration-ha-mcp/937847)** — Community discussion thread
