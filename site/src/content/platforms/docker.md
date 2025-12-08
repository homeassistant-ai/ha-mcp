---
name: Docker
icon: docker
order: 4
---

## Requirements

Docker or Docker Desktop must be installed and running.

## Client Configuration

Add this to your AI client's MCP configuration:

```json
{
  "mcpServers": {
    "home-assistant": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-e", "HOMEASSISTANT_URL=http://host.docker.internal:8123",
        "-e", "HOMEASSISTANT_TOKEN=your_long_lived_token",
        "ghcr.io/homeassistant-ai/ha-mcp:latest"
      ]
    }
  }
}
```

**Note:** Use `host.docker.internal` to access Home Assistant running on your host machine.

## Quick Test

Try the demo environment:
```bash
docker run --rm -i \
  -e HOMEASSISTANT_URL=https://ha-mcp-demo-server.qc-h.net \
  -e HOMEASSISTANT_TOKEN=demo \
  ghcr.io/homeassistant-ai/ha-mcp:latest
```

## Troubleshooting

Having SSL certificate issues? Check the [FAQ](/faq#ssl-certificates).
