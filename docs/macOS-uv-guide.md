# macOS Quick Start

Control Home Assistant with Claude Desktop in 2 minutes.

**Works with free Claude account** - no subscription needed.

---

## Step 1: Get Claude Desktop

1. Download **Claude Desktop** from [claude.ai/download](https://claude.ai/download)
2. Install and open it
3. Create a free account (or sign in)

---

## Step 2: Run the Installer

Open **Terminal** and paste:

```bash
curl -LsSf https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/main/scripts/install-macos.sh | bash
```

This installs `uv` (if needed) and configures Claude Desktop for the demo environment.

---

## Step 3: Restart & Test

1. **Quit Claude completely** - Press **Cmd+Q** (not just close the window)
2. **Reopen Claude Desktop**
3. **Test it** - Ask Claude:

```
Can you see my Home Assistant?
```

Claude should list entities from the demo environment.

---

## Step 4: Explore the Demo

The demo environment is a real Home Assistant you can experiment with:

- **Web UI**: https://ha-mcp-demo-server.qc-h.net
- **Login**: `mcp` / `mcp`
- **Resets weekly** - your changes won't persist

Try asking Claude:
- "Turn on the kitchen lights"
- "What's the temperature in the living room?"
- "Create an automation that turns off all lights at midnight"

---

## Step 5: Connect Your Home Assistant

Ready to use your own Home Assistant? Edit the config file:

```bash
open "$HOME/Library/Application Support/Claude/claude_desktop_config.json"
```

Replace the demo values:

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

**To get your token:**
1. Open Home Assistant in browser
2. Click your username (bottom left)
3. **Security** tab → **Long-lived access tokens**
4. Create token → Copy immediately (shown only once)

Then restart Claude (Cmd+Q, reopen).

---

<details>
<summary><strong>Manual Installation</strong> (if the installer doesn't work)</summary>

### Install uv

```bash
brew install uv
```

Or without Homebrew:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Configure Claude Desktop

1. Open Claude Desktop
2. Menu bar → **Claude** → **Settings...** → **Developer** → **Edit Config**
3. Paste:

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

4. Save and restart Claude (Cmd+Q, reopen)

</details>

---

## Problems?

See the [FAQ & Troubleshooting Guide](FAQ.md) for common issues.
