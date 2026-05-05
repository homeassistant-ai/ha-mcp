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
| Tools | ~15 basic tools | 92+ comprehensive tools |
| Focus | Device control | Full system administration |
| Automations | Limited | Create, edit, debug, trace |
| Dashboards | No | Full dashboard management |
| Cameras | No | Screenshot and analysis |

Built-in = operate devices. ha-mcp = administer your system.

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

### OAuth stopped working after upgrading to v7.0.0

v7.0.0 removed the Home Assistant URL field from the OAuth consent form to fix security vulnerabilities (SSRF and XSS). Set `HOMEASSISTANT_URL` as a server-side environment variable before starting ha-mcp. See the [OAuth migration guide](OAUTH.md#migrating-from-v6x) for instructions.

### Claude.ai says "Couldn't reach the MCP server"

**This is normal.** Claude.ai shows this error during its initial connection handshake, but the server connects successfully afterward. To verify you're actually connected:

1. Look for a **"Configure"** button on the connector — click it
2. If you see tools listed, you're connected and ready to go

You can also start a new conversation and ask Claude if it can see your Home Assistant via the MCP connection — this is the easiest way to confirm it's truly connected. Checking your server logs for successful requests (HTTP 200) after the initial error also confirms the connection is working.

This is a known Claude.ai behavior that affects all MCP servers, not just ha-mcp.

### "Terminating session: None" in server logs

**This is normal.** ha-mcp runs in stateless HTTP mode, which means each request creates and discards a temporary session. The `Terminating session: None` log message is the MCP SDK reporting this routine cleanup — the connection stays active.

### Cloudflare: LLM can't connect ("Block AI training bots")

If you're using Cloudflare and your LLM client can't connect to the MCP server (but visiting the URL in your browser works), Cloudflare's **"Block AI training bots"** setting is almost certainly the cause. This is the most common connection issue for Cloudflare users.

To disable it:

1. Log in to [Cloudflare](https://dash.cloudflare.com)
2. In the left sidebar, click **Domains**, then click **Overview**
3. Click on the domain you use for connecting to Home Assistant
4. On the right side of the page, find **"Control AI Crawlers"**
5. Under **"Block AI training bots"**, open the dropdown
6. Select **"do not block (allow crawlers)"**

![Cloudflare AI Crawlers Setting](https://homeassistant-ai.github.io/ha-mcp/images/cloudflare-ai-crawlers-setting.jpg)

See [#783](https://github.com/homeassistant-ai/ha-mcp/issues/783) for more details.

### macOS: "All connection attempts failed" to local Home Assistant

If ha-mcp connects to the demo server but fails to reach your local Home Assistant (`192.168.x.x`, `10.x.x.x`, etc.) on macOS, the most common causes are listed below. See [#867](https://github.com/homeassistant-ai/ha-mcp/issues/867) (Local Network Privacy), [#630](https://github.com/homeassistant-ai/ha-mcp/issues/630) (env vars not reaching ha-mcp), and [#773](https://github.com/homeassistant-ai/ha-mcp/issues/773) (Python version/read-only filesystem) for related reports.

**1. macOS Local Network Privacy (Sequoia 15+)**

macOS Sequoia silently blocks subprocess connections to local network IPs. Claude Desktop spawns `uvx` as a child process, and macOS may block its outbound LAN connections without showing a permission dialog.

- Check **System Settings → Privacy & Security → Local Network** for Claude Desktop
- If Claude Desktop is not listed, try restarting it to trigger the permission prompt

**Workaround — SSH tunnel to localhost:**

Since macOS does not restrict connections to `localhost`, an SSH port forward bypasses the restriction:

```bash
ssh -N -L 8123:localhost:8123 user@your-ha-server-ip
```

Then set `HOMEASSISTANT_URL` to `http://localhost:8123` in your config.

**2. Firewall software (Little Snitch, Lulu, etc.)**

Third-party firewalls may block `python` or `node` processes spawned by Claude Desktop from making network connections. Check your firewall rules and allow connections for these processes. See [#780](https://github.com/homeassistant-ai/ha-mcp/issues/780) for an example resolution.

**3. http:// vs https://**

Home Assistant running in container mode (Docker, K3s) uses HTTP by default. Using `https://` causes a TLS handshake error. Use `http://` unless you have explicitly configured SSL/TLS or a reverse proxy.

**4. Python version too old**

ha-mcp requires Python 3.13+. If you are on Python 3.12 or older, `uvx` installs an outdated version of ha-mcp that may have known bugs (including read-only filesystem errors). Upgrade Python:

```bash
brew install python@3.13
```

Then force a refresh:

```bash
uvx --refresh ha-mcp@latest
```

If `uvx` still uses the old Python after installing 3.13, explicitly pin it by adding `--python 3.13` to your config args:

```json
"args": ["--python", "3.13", "ha-mcp@latest"]
```

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

### Windows: pywin32 installation fails

If you see `Failed to install: pywin32` or `os error 32` ("file is used by another process") when starting ha-mcp on Windows, this is caused by two upstream bugs:

1. The MCP Python SDK requires `pywin32` on Windows even though server-only users don't need it ([python-sdk#2233](https://github.com/modelcontextprotocol/python-sdk/issues/2233))
2. `uv` has a known issue installing `pywin32` on Windows ([uv#17679](https://github.com/astral-sh/uv/issues/17679))

**Workaround — use Docker:**

```json
{
  "mcpServers": {
    "Home Assistant": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-e", "HOMEASSISTANT_URL=http://host.docker.internal:8123",
        "-e", "HOMEASSISTANT_TOKEN=your_token",
        "ghcr.io/homeassistant-ai/ha-mcp:latest"
      ]
    }
  }
}
```

See [#672](https://github.com/homeassistant-ai/ha-mcp/issues/672) for details.

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
   - Windows (traditional installer): `%APPDATA%\Claude\claude_desktop_config.json`
   - Windows (Microsoft Store): path varies by package — see the [Windows setup guide](https://homeassistant-ai.github.io/ha-mcp/guide-windows) for a detection snippet
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

### Can't connect remotely? Try the Webhook Proxy add-on {#webhook-proxy}

If you're having trouble setting up remote access — TLS errors, Cloudflare configuration issues, or port forwarding problems — the **Webhook Proxy add-on** may be a simpler alternative.

Instead of requiring a dedicated tunnel to port 9583, the Webhook Proxy routes MCP traffic through Home Assistant's main port (8123) via a webhook. If you already have **Nabu Casa** or any reverse proxy pointing at your HA instance, this can be the easiest remote setup.

1. Install the **MCP Server add-on** and the **Webhook Proxy add-on** from the add-on store
2. Start the webhook proxy and restart Home Assistant when prompted
3. Copy the webhook URL from the add-on logs
4. Use that URL in your MCP client configuration

See [#784](https://github.com/homeassistant-ai/ha-mcp/issues/784) for an example where this resolved a TLS connection issue.

### Server works but responses are slow

1. **First request is slow** - `uvx` downloads packages on first run
2. **Subsequent requests** - Should be faster (packages cached)
3. **Alternative** - Use Docker for consistent performance

### Tools are missing or using old version

If you're seeing fewer tools than expected or outdated behavior, `uvx` may be using a cached old version.

**Solution:**

```bash
# Clear the uv cache
uv cache clean

# Force refresh to latest version
uvx --refresh ha-mcp@latest
```

**Verify the version:**
```bash
uvx ha-mcp@latest --version
```

The version should match the [latest release](https://github.com/homeassistant-ai/ha-mcp/releases/latest). If you see a much older version, the cache needs clearing.

---

---

## Configuration Options

### Environment Variables

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `HOMEASSISTANT_URL` | Your Home Assistant URL | - | Yes |
| `HOMEASSISTANT_TOKEN` | Long-lived access token (or `demo` for demo env) | - | Yes |
| `BACKUP_HINT` | Backup recommendation level | `normal` | No |

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
