# Dev Channel

Want to test the latest changes before the weekly Tuesday release? The dev channel provides early access to features, bug fixes, and improvements as soon as they're merged to master.

**Who should use this:**
- Contributors testing their PRs
- Issue reporters verifying fixes
- Early adopters wanting the latest features

**Important:** Dev releases (`.devN`) may contain bugs. For production use, stick with stable releases.

## Release Schedule

| Channel | When Updated | Package/Tag |
|---------|--------------|-------------|
| **Dev** | Every push to master | `ha-mcp-dev` / `:dev` |
| **Stable** | Weekly (Tuesday 10:00 UTC) | `ha-mcp` / `:latest` |

## Installation Methods

### uvx (Recommended)

The simplest method - no installation required, no virtual environments, just run:

```bash
# Run dev version directly
uvx ha-mcp-dev
```

**Check version:**
```bash
uvx ha-mcp-dev --version
# Output: ha-mcp 6.3.1.dev140
```

**Switch back to stable:**
```bash
uvx ha-mcp
```

**Config changes required:** None. Uses the same `HOMEASSISTANT_URL` and `HOMEASSISTANT_TOKEN` environment variables.

### pip (Alternative)

If you prefer installing the package:

```bash
# Install system-wide (requires sudo/admin on some systems)
pip install --user ha-mcp-dev

# Run the installed package
ha-mcp-dev
```

**Check version:**
```bash
pip show ha-mcp-dev | grep Version
```

**Switch back to stable:**
```bash
pip uninstall ha-mcp-dev -y
pip install --user ha-mcp
```

### uv (Alternative)

Using uv's package installer:

```bash
# Create a virtual environment (one-time setup)
uv venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dev version
uv pip install ha-mcp-dev

# Run
ha-mcp-dev
```

**Check version:**
```bash
uv pip show ha-mcp-dev | grep Version
```

**Switch back to stable:**
```bash
uv pip uninstall ha-mcp-dev
uv pip install ha-mcp
```

### Docker

Dev images are published to GitHub Container Registry with the `dev` tag.

**Pull the dev image:**
```bash
docker pull ghcr.io/homeassistant-ai/ha-mcp:dev
```

**Run in stdio mode (Claude Desktop):**
```bash
docker run --rm -i \
  -e HOMEASSISTANT_URL=http://your-ha-instance:8123 \
  -e HOMEASSISTANT_TOKEN=your_token \
  ghcr.io/homeassistant-ai/ha-mcp:dev
```

**Run in HTTP mode (web clients):**
```bash
docker run -d -p 8086:8086 \
  -e HOMEASSISTANT_URL=http://your-ha-instance:8123 \
  -e HOMEASSISTANT_TOKEN=your_token \
  ghcr.io/homeassistant-ai/ha-mcp:dev ha-mcp-web
```

**Config changes required:** Change the image tag from `latest` to `dev`:
```diff
- ghcr.io/homeassistant-ai/ha-mcp:latest
+ ghcr.io/homeassistant-ai/ha-mcp:dev
```

**Switch back to stable:**
```bash
docker pull ghcr.io/homeassistant-ai/ha-mcp:latest
# Stop your dev container and start a new one with :latest tag
```

### Home Assistant Add-on

The dev channel is available as a **separate add-on** in the Home Assistant add-on store.

**To use the dev channel:**

1. Open Home Assistant
2. Go to **Settings** → **Add-ons** → **Add-on Store**
3. Search for **"Home Assistant MCP Server (Dev)"**
4. Click **Install**
5. Configure with your token (if not using auto-discovery)
6. Start the add-on

**Key differences from stable add-on:**

| Property | Stable | Dev |
|----------|--------|-----|
| **Name** | Home Assistant MCP Server | Home Assistant MCP Server (Dev) |
| **Slug** | `ha_mcp` | `ha_mcp_dev` |
| **Stage** | Stable | Experimental |
| **Updates** | Weekly (Tuesday) | Every master push |

**Can I run both?** Yes! Both add-ons can be installed simultaneously. They use different slugs and configuration.

**Switch back to stable:** Simply stop the dev add-on and start/install the stable "Home Assistant MCP Server" add-on instead.

### Claude Desktop Configuration

If you're using Claude Desktop, update your `claude_desktop_config.json`:

**Location:**
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

**For uvx (dev channel):**
```json
{
  "mcpServers": {
    "ha-mcp": {
      "command": "uvx",
      "args": ["ha-mcp-dev"],
      "env": {
        "HOMEASSISTANT_URL": "http://your-ha-instance:8123",
        "HOMEASSISTANT_TOKEN": "your_token"
      }
    }
  }
}
```

**Config changes required:** Change `"ha-mcp"` to `"ha-mcp-dev"` in the args array.

**Switch back to stable:** Change `"ha-mcp-dev"` back to `"ha-mcp"` in the args array, then restart Claude Desktop.

## Checking Your Version

The easiest way to check your version:

```bash
# With uvx
uvx ha-mcp-dev --version

# With pip
pip show ha-mcp-dev | grep Version

# With uv
uv pip show ha-mcp-dev | grep Version

# With Docker
docker run --rm ghcr.io/homeassistant-ai/ha-mcp:dev ha-mcp --version
```

Dev versions follow the format: `6.3.1.dev140` where `.dev140` indicates the 140th commit since the last stable release.

## Reporting Issues

If you encounter issues with a dev release:

1. **Note the exact version number** (e.g., `6.3.1.dev140`)
2. **Check if the issue exists in stable** - Try the [latest stable release](https://github.com/homeassistant-ai/ha-mcp/releases/latest) to confirm it's dev-specific
3. **Report the issue:**
   - If it's a regression (worked in stable, broken in dev), comment on the related PR or issue
   - If it's a new bug, [open a bug report](https://github.com/homeassistant-ai/ha-mcp/issues/new?template=bug_report.md) and include:
     - The dev version number
     - Whether it reproduces in stable
     - Steps to reproduce

## See Also

- [Main Documentation](../README.md)
- [Setup Wizard](https://homeassistant-ai.github.io/ha-mcp/setup/)
- [FAQ & Troubleshooting](https://homeassistant-ai.github.io/ha-mcp/faq/)
- [Contributing Guide](../CONTRIBUTING.md)
