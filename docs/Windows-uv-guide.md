# Windows UV Setup Guide for ha-mcp

_Based on steps shared by @kingbear2._

This guide walks through running the ha-mcp server locally on Windows with Claude using the [uv](https://docs.astral.sh/uv/) package manager. Expect the process to take about 10 minutes.

## 1. Install uv

Open **PowerShell** or **cmd** and run:

```powershell
winget install astral-sh.uv -e
```

## 2. Configure Claude Desktop

1. Open **Claude Desktop → Settings → Developer → Edit Config**.
2. Replace `claude_desktop_config.json` with:

    ```json
    {
      "mcpServers": {
        "Home Assistant": {
          "command": "uvx",
          "args": ["ha-mcp"],
          "env": {
            "HOMEASSISTANT_URL": "http://homeassistant.local:8123",
            "HOMEASSISTANT_TOKEN": "your_long_lived_token"
          }
        }
      }
    }
    ```

- HOMEASSISTANT_URL: use the same url (https or http) that you use for accessing home assistant
- HOMEASSISTANT_TOKEN: : click your username on the bottom left of HA, click Security at the top, and scroll all the way down and create a new token. Note: this token will be displayed only once

3. Exit Claude completely, then relaunch it. Under **Settings → Developer** you should see the MCP server running.

Ask Claude to verify access (e.g., “Can you see my Home Assistant interface?”). If it enumerates integrations or entities, the setup succeeded.

## Troubleshooting

- **`uvx` not found:** Re-run the PATH export or use the full path to `uvx.exe`.
- **Authentication failures:** Regenerate the long-lived token and update Claude’s config.
- **Server closes immediately:** Check the console log for missing dependencies or incorrect configuration.
