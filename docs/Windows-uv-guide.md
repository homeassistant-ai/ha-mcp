# Windows Quick Start

Control Home Assistant with Claude Desktop in 2 minutes.

**Works with free Claude account** - no subscription needed.

---

## Step 1: Create a Claude Account

Go to [claude.ai](https://claude.ai) and create a free account (or sign in if you have one).

---

## Step 2: Run the Installer

Open **PowerShell** and paste:

```powershell
irm https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/main/scripts/install-windows.ps1 | iex
```

This installs `uv` (if needed) and configures Claude Desktop for the demo environment.

---

## Step 3: Install or Restart Claude Desktop

Download and install **Claude Desktop** from [claude.ai/download](https://claude.ai/download).

Already have it? Restart it: **File → Exit** (or Alt+F4), then reopen.

---

## Step 4: Test It

Open Claude Desktop and ask:

```
Can you see my Home Assistant?
```

Claude should list entities from the demo environment.

---

## Step 5: Explore the Demo

The demo environment is a real Home Assistant you can experiment with:

- **Web UI**: https://ha-mcp-demo-server.qc-h.net
- **Login**: `mcp` / `mcp`
- **Resets weekly** - your changes won't persist

Try asking Claude:
- "Turn on the kitchen lights"
- "What's the temperature in the living room?"
- "Create an automation that turns off all lights at midnight"

---

## Step 6: Connect Your Home Assistant

Ready to use your own Home Assistant? Edit the config file:

```powershell
notepad "$env:APPDATA\Claude\claude_desktop_config.json"
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

Then restart Claude (Alt+F4, reopen).

---

## Step 7: Share Your Feedback

We'd love to hear how you're using ha-mcp!

- **[GitHub Discussions](https://github.com/homeassistant-ai/ha-mcp/discussions)** — Share your automations, ask questions
- **[GitHub Issues](https://github.com/homeassistant-ai/ha-mcp/issues)** — Report bugs or request features

---

<details>
<summary><strong>Manual Installation</strong> (if the installer doesn't work)</summary>

### Install uv

Open **PowerShell** or **cmd**:

```powershell
winget install astral-sh.uv -e
```

### Configure Claude Desktop

1. Open Claude Desktop
2. **Settings** → **Developer** → **Edit Config**
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

4. Save and restart Claude (Alt+F4, reopen)

</details>

---

## Problems?

See the [FAQ & Troubleshooting Guide](FAQ.md) for common issues.
