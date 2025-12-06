---
name: uvx (Python)
description: Run ha-mcp directly via Python package manager
icon: python
forConnections: ['local', 'network']
order: 1
---

## For Local Machine (stdio)

ha-mcp is spawned by your AI client. Just configure your client with:

```bash
command: uvx
args: ["ha-mcp@latest"]
```

No separate server setup needed - your client handles it.

## For Local Network (HTTP Server)

Run ha-mcp as an HTTP server:

```bash
export HOMEASSISTANT_URL=http://homeassistant.local:8123
export HOMEASSISTANT_TOKEN=your_long_lived_token
export MCP_PORT=8086
uvx --from ha-mcp@latest ha-mcp-web
```

Server will be available at: `http://YOUR_IP:8086/mcp`

### Run in Background

```bash
nohup uvx --from ha-mcp@latest ha-mcp-web > /dev/null 2>&1 &
```

### With Custom Secret Path

For additional security:

```bash
export MCP_SECRET_PATH=/__my_secret_path__
uvx --from ha-mcp@latest ha-mcp-web
```

Server URL: `http://YOUR_IP:8086/__my_secret_path__`

## Requirements

- uv installed (see Platform instructions)
- Python 3.10+ (managed by uv automatically)
