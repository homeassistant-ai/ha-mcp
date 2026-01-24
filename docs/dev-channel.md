# Dev Channel

Want to test the latest changes before they hit stable? Dev releases are published automatically on every push to the `master` branch.

## What is the Dev Channel?

The dev channel provides early access to:
- **New features** before they're included in stable releases
- **Bug fixes** as soon as they're merged
- **Performance improvements** and optimizations

## Release Schedule

| Channel | When Updated | Package |
|---------|--------------|---------|
| **Dev** | Every push to master | `ha-mcp-dev` |
| **Stable** | Weekly (Tuesday 10:00 UTC) | `ha-mcp` |

## Installation Methods

### pip / uvx

Dev releases are published as a separate package called `ha-mcp-dev`:

```bash
# Install dev version with pip
pip install ha-mcp-dev

# Install dev version with uv
uv pip install ha-mcp-dev

# Run directly with uvx
uvx ha-mcp-dev
```

**Config changes required:** None. The same `HOMEASSISTANT_URL` and `HOMEASSISTANT_TOKEN` environment variables work with dev releases.

**Switch back to stable:**

To reliably switch to the latest stable version, it's best to uninstall the dev package and then install the stable package.

```bash
# With pip
pip uninstall ha-mcp-dev -y && pip install ha-mcp

# With uv
uv pip uninstall ha-mcp-dev && uv pip install ha-mcp
```

### Docker

<!-- TODO: Verify this works -->

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

Pull the `latest` image, then stop your `dev` container and start a new one using the `latest` tag.

```bash
docker pull ghcr.io/homeassistant-ai/ha-mcp:latest
```

### Home Assistant Add-on

<!-- TODO: Verify this works -->

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

**Switch back to stable:** Change `"ha-mcp-dev"` back to `"ha-mcp"` in the args array.

## Checking Your Version

To verify which version you're running:

```bash
# Check installed version (dev package)
pip show ha-mcp-dev | grep Version

# Check installed version (stable package)
pip show ha-mcp | grep Version

# Or with uv
uv pip show ha-mcp-dev | grep Version
```

## Reporting Issues

If you encounter issues with a dev release:

1. Note the exact version number
2. Check if the issue exists in the [latest stable release](https://github.com/homeassistant-ai/ha-mcp/releases/latest)
3. If it's a dev-only issue, [open a bug report](https://github.com/homeassistant-ai/ha-mcp/issues/new?template=bug_report.md) with the dev version number

## See Also

- [Main Documentation](../README.md)
- [Setup Wizard](https://homeassistant-ai.github.io/ha-mcp/setup/)
- [FAQ & Troubleshooting](./FAQ.md)
- [Contributing Guide](../CONTRIBUTING.md)
