# Dev Channel

Want to test the latest changes before they hit stable? Dev releases (`.devN`) are published automatically on every push to the `master` branch.

## What is the Dev Channel?

The dev channel provides early access to:
- **New features** before they're included in stable releases
- **Bug fixes** as soon as they're merged
- **Performance improvements** and optimizations

Dev releases follow the naming convention `X.Y.Z.devN` (e.g., `6.3.1.dev5`).

## Release Schedule

| Channel | When Updated | Version Format |
|---------|--------------|----------------|
| **Dev** | Every push to master | `X.Y.Z.devN` |
| **Stable** | Weekly (Tuesday 10:00 UTC) | `X.Y.Z` |

## Installation Methods

### pip / uvx

If you installed ha-mcp via pip or uvx, switch to the dev channel by adding the `--pre` flag:

```bash
# Install dev version with pip
pip install ha-mcp --pre

# Install dev version with uv
uv pip install ha-mcp --pre

# Run directly with uvx (always gets latest dev)
uvx --prerelease=allow ha-mcp
```

**Config changes required:** None. The same `HOMEASSISTANT_URL` and `HOMEASSISTANT_TOKEN` environment variables work with dev releases.

**Switch back to stable:**

To reliably switch to the latest stable version, it's best to uninstall and then reinstall the package.

```bash
# With pip
pip uninstall ha-mcp -y && pip install ha-mcp

# With uv
uv pip uninstall ha-mcp && uv pip install ha-mcp

### Docker

Dev images are published to GitHub Container Registry with the `dev` tag:

```bash
# Pull the dev image
docker pull ghcr.io/homeassistant-ai/ha-mcp:dev

# Run in stdio mode (Claude Desktop)
docker run --rm -i \
  -e HOMEASSISTANT_URL=http://your-ha-instance:8123 \
  -e HOMEASSISTANT_TOKEN=your_token \
  ghcr.io/homeassistant-ai/ha-mcp:dev

# Run in HTTP mode (web clients)
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
```

### Home Assistant Add-on

The add-on supports switching between stable and dev channels via configuration.

1. Open the add-on configuration in Home Assistant
2. Change the `channel` option:

```yaml
# For dev channel
channel: dev

# For stable channel (default)
channel: stable
```

3. Restart the add-on

**Config changes required:** Add or modify the `channel` configuration option.

**Switch back to stable:** Set `channel: stable` or remove the channel option entirely (stable is the default).

### Claude Desktop Configuration

If you're using Claude Desktop with a manual configuration, update your `claude_desktop_config.json`:

**For uvx (dev channel):**
```json
{
  "mcpServers": {
    "ha-mcp": {
      "command": "uvx",
      "args": ["--prerelease=allow", "ha-mcp"],
      "env": {
        "HOMEASSISTANT_URL": "http://your-ha-instance:8123",
        "HOMEASSISTANT_TOKEN": "your_token"
      }
    }
  }
}
```

**Config changes required:** Add `"--prerelease=allow"` to the args array before `"ha-mcp"`.

**Switch back to stable:** Remove `"--prerelease=allow"` from the args array.

## Checking Your Version

To verify which version you're running:

```bash
# Check installed version
pip show ha-mcp | grep Version

# Or with uv
uv pip show ha-mcp | grep Version

# Or run ha-mcp directly
ha-mcp --version
```

## Reporting Issues

If you encounter issues with a dev release:

1. Note the exact version number (e.g., `6.3.1.dev5`)
2. Check if the issue exists in the [latest stable release](https://github.com/homeassistant-ai/ha-mcp/releases/latest)
3. If it's a dev-only issue, [open a bug report](https://github.com/homeassistant-ai/ha-mcp/issues/new?template=bug_report.md) with the dev version number

## See Also

- [Main Documentation](../README.md)
- [Setup Wizard](https://homeassistant-ai.github.io/ha-mcp/setup/)
- [FAQ & Troubleshooting](./FAQ.md)
- [Contributing Guide](../CONTRIBUTING.md)
