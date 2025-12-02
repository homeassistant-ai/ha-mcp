# macOS UV Setup Guide for ha-mcp

This guide walks through running the ha-mcp server locally on macOS with Claude using the [uv](https://docs.astral.sh/uv/) package manager. Expect the process to take about 10 minutes.

## Prerequisites

- [Claude Desktop](https://claude.ai/download) - Works with a free Claude account
- A Home Assistant instance with a long-lived access token

## 1. Install uv

Open **Terminal** and run one of the following:

**Using Homebrew (recommended if you have Homebrew):**

```bash
brew install uv
```

**Using the standalone installer:**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

After installation, verify it works:

```bash
uvx --version
```

## 2. Configure Claude Desktop

1. Open **Claude Desktop**.
2. Click **Claude** in the menu bar → **Settings...** → **Developer** → **Edit Config**.
3. Replace the contents of `claude_desktop_config.json` with:

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

- **HOMEASSISTANT_URL**: Use the same URL (https or http) that you use for accessing Home Assistant
- **HOMEASSISTANT_TOKEN**: Click your username on the bottom left of HA, click Security at the top, and scroll all the way down and create a new token. Note: this token will be displayed only once

4. Quit Claude completely (**Cmd+Q**), then relaunch it. Under **Settings → Developer** you should see the MCP server running.

Ask Claude to verify access (e.g., "Can you see my Home Assistant interface?"). If it enumerates integrations or entities, the setup succeeded.

## Troubleshooting

- **`uvx` not found:** If you used the curl installer, you may need to restart Terminal or run `source ~/.zshrc` (or `~/.bashrc`) to update your PATH.
- **Authentication failures:** Regenerate the long-lived token and update Claude's config.
- **Server closes immediately:** Check the console log for missing dependencies or incorrect configuration.
- **Homebrew not installed:** Install it from [brew.sh](https://brew.sh) or use the curl installer method above.
