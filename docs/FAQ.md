# FAQ & Troubleshooting

Common questions and solutions for ha-mcp setup.

## General Questions

### Do I need the Home Assistant Add-on?

**No.** The HA add-on is just one installation method. Most users run ha-mcp directly on their computer using `uvx` (recommended for Claude Desktop). The add-on is only needed if you want to run ha-mcp inside your Home Assistant OS environment.

### Do I need a Claude Pro subscription?

**No.** Claude Desktop works with a free Claude account. The MCP integration is available to all users.

### What's the difference between ha-mcp and Home Assistant's built-in MCP?

| Feature | Built-in HA MCP | ha-mcp |
|---------|-----------------|--------|
| Tools | ~7 basic tools | 80+ comprehensive tools |
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

### MCP server not showing in Claude Desktop

1. **Restart Claude completely** - Use Cmd+Q (Mac) or Alt+F4 (Windows), not just close the window
2. **Check config file location:**
   - Mac: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
3. **Verify JSON syntax** - No trailing commas, proper quotes
4. **Check the MCP icon** - Bottom left of Claude Desktop shows connected servers

### "uvx not found" error

**Mac:**
```bash
# If using curl installer, reload shell
source ~/.zshrc
# Or use full path
~/.local/bin/uvx --version
```

**Windows:**
```powershell
# Restart PowerShell/cmd after installing uv
# Or use full path
%USERPROFILE%\.local\bin\uvx.exe --version
```

### "Connection refused" or timeout errors

1. **Verify Home Assistant is running** - Open HOMEASSISTANT_URL in a browser
2. **Check the URL format** - Include `http://` or `https://` and the port (usually `:8123`)
3. **Network access** - Ensure your computer can reach Home Assistant
4. **Docker users** - Use `host.docker.internal` instead of `localhost`

### "Token invalid" or authentication errors

1. **Generate a new token:**
   - Home Assistant → Click your username (bottom left)
   - Security tab → Long-lived access tokens
   - Create Token → Copy immediately (shown only once)
2. **Check token format** - Don't wrap the token in quotes in your config
3. **Token expiration** - Tokens don't expire by default, but can be revoked

### Claude says it can't see Home Assistant

1. Click the **MCP server icon** (bottom left in Claude Desktop)
2. Check if "Home Assistant" is listed
3. If not listed, check your config file syntax
4. Try asking: "Can you list your available tools?"

### Server works but responses are slow

1. **First request is slow** - `uvx` downloads packages on first run
2. **Subsequent requests** - Should be faster (packages cached)
3. **Alternative** - Use Docker for consistent performance

### "Entity not found" errors

1. Entity IDs are case-sensitive
2. Use the search tool: "Search for kitchen light"
3. Check if the entity exists in Home Assistant Developer Tools → States

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
