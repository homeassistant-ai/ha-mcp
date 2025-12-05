# macOS Setup Guide

Get ha-mcp running with Claude Desktop in about 5 minutes.

**Works with free Claude account** - no subscription needed.

## 1. Install uv

Open **Terminal** and run:

```bash
brew install uv
```

Don't have Homebrew? Use the standalone installer instead:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 2. Configure Claude Desktop

1. Open **Claude Desktop**
2. Menu bar → **Claude** → **Settings...** → **Developer** → **Edit Config**
3. Paste this configuration:

```json
{
  "mcpServers": {
    "Home Assistant": {
      "command": "uvx",
      "args": ["ha-mcp@latest"],
      "env": {
        "HOMEASSISTANT_URL": "http://homeassistant.local:8123",
        "HOMEASSISTANT_TOKEN": "your_long_lived_token"
      }
    }
  }
}
```

**Replace:**
- `HOMEASSISTANT_URL` - Your Home Assistant URL (same one you use in browser)
- `HOMEASSISTANT_TOKEN` - Generate in HA: Your Profile → Security → Long-lived access tokens

## 3. Restart & Test

1. Quit Claude completely (**Cmd+Q**)
2. Reopen Claude Desktop
3. Ask: **"Can you see my Home Assistant?"**

If Claude lists your entities, you're done!

---

## Try the Demo First

Don't have Home Assistant yet? Use our public demo environment:

```json
{
  "mcpServers": {
    "Home Assistant": {
      "command": "uvx",
      "args": ["ha-mcp@latest"],
      "env": {
        "HOMEASSISTANT_URL": "https://ha-mcp-demo-server.qc-h.net",
        "HOMEASSISTANT_TOKEN": "demo"
      }
    }
  }
}
```

Web UI: https://ha-mcp-demo-server.qc-h.net (login: `mcp` / `mcp`)

The demo resets weekly - your changes won't persist.

---

## Problems?

See the [FAQ & Troubleshooting Guide](FAQ.md) for common issues.
