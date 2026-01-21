---
name: Docker
description: Run ha-mcp in a container
icon: docker
forConnections: ['local', 'network', 'remote']
order: 2
---

## For Local Machine (stdio)

Use Docker in your AI client config:

```json
{
  "mcpServers": {
    "home-assistant": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-e", "HOMEASSISTANT_URL=http://host.docker.internal:8123",
        "-e", "HOMEASSISTANT_TOKEN=your_token",
        "ghcr.io/homeassistant-ai/ha-mcp:latest"
      ]
    }
  }
}
```

**Note:** Use `host.docker.internal` to access services on your host machine.

## For Local Network (HTTP Server)

Run as a persistent HTTP server:

```bash
docker run -d --name ha-mcp \
  -p 8086:8086 \
  -e HOMEASSISTANT_URL=http://homeassistant.local:8123 \
  -e HOMEASSISTANT_TOKEN=your_token \
  ghcr.io/homeassistant-ai/ha-mcp:latest \
  ha-mcp-web
```

Server will be available at: `http://YOUR_IP:8086/mcp`

**Customize port and path:**

```bash
docker run -d --name ha-mcp \
  -p 9000:9000 \
  -e HOMEASSISTANT_URL=http://homeassistant.local:8123 \
  -e HOMEASSISTANT_TOKEN=your_token \
  -e MCP_PORT=9000 \
  -e MCP_SECRET_PATH=/my-secret-path \
  ghcr.io/homeassistant-ai/ha-mcp:latest \
  ha-mcp-web
```

Server URL: `http://YOUR_IP:9000/my-secret-path`

> **Note:** Both the Docker port mapping (`-p 9000:9000`) and the `MCP_PORT` environment variable must match the desired port. The first number in `-p` is the host port, the second is the container port (which matches `MCP_PORT`).

### Management Commands

```bash
# View logs
docker logs ha-mcp -f

# Stop server
docker stop ha-mcp

# Remove container
docker rm ha-mcp

# Update to latest
docker pull ghcr.io/homeassistant-ai/ha-mcp:latest
```

## For Remote Access (OAuth Mode)

OAuth mode enables **secure, multi-user authentication** via a consent form. Ideal for Claude.ai and other remote clients.

```bash
docker run -d --name ha-mcp-oauth \
  -p 8086:8086 \
  -e MCP_BASE_URL=https://your-public-url.com \
  -e OAUTH_ENCRYPTION_KEY=your-32-byte-base64-key \
  ghcr.io/homeassistant-ai/ha-mcp:latest \
  ha-mcp-oauth
```

Server URL: `https://your-public-url.com/mcp`

### OAuth Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `MCP_BASE_URL` | Public HTTPS URL (required for OAuth) | `http://localhost:8086` |
| `MCP_PORT` | Server port | `8086` |
| `MCP_SECRET_PATH` | Endpoint path | `/mcp` |
| `OAUTH_ENCRYPTION_KEY` | Token encryption key | Auto-generated |
| `LOG_LEVEL` | Logging verbosity | `INFO` |

> **Note:** In OAuth mode, `HOMEASSISTANT_URL` and `HOMEASSISTANT_TOKEN` are NOT required. Users provide their credentials via the consent form.

### Generate Encryption Key

For production, generate a persistent encryption key:

```bash
# Generate key
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Example output: gAAAAABn...
```

Without a persistent key, all user sessions are invalidated when the container restarts.

### OAuth with Cloudflare Tunnel

Expose OAuth server with HTTPS:

```bash
# Start ha-mcp in OAuth mode
docker run -d --name ha-mcp-oauth \
  -p 8086:8086 \
  -e MCP_BASE_URL=https://ha-mcp.yourdomain.com \
  -e OAUTH_ENCRYPTION_KEY=your-key \
  ghcr.io/homeassistant-ai/ha-mcp:latest \
  ha-mcp-oauth

# Start Cloudflare tunnel
cloudflared tunnel --url http://localhost:8086
```

---

## Custom SSL Certificates

If your Home Assistant is behind a reverse proxy with self-signed certificates, you need to provide a CA bundle that includes both your custom CA and the standard root CAs.

### Create Combined CA Bundle

```bash
# Get the default CA bundle and append your custom CA
cat $(python3 -m certifi) /path/to/your-ca.crt > combined-ca-bundle.crt
```

### Run with Custom CA

```bash
docker run -d --name ha-mcp \
  -p 8086:8086 \
  -e HOMEASSISTANT_URL=https://homeassistant.example.com \
  -e HOMEASSISTANT_TOKEN=your_token \
  -e SSL_CERT_FILE=/certs/ca-bundle.crt \
  -v /path/to/combined-ca-bundle.crt:/certs/ca-bundle.crt:ro \
  ghcr.io/homeassistant-ai/ha-mcp:latest \
  ha-mcp-web
```

---

## Server Mode Comparison

| Mode | Command | Use Case | Auth Method |
|------|---------|----------|-------------|
| **stdio** | (default) | Claude Desktop, local clients | Pre-configured token |
| **ha-mcp-web** | HTTP server | LAN clients, single user | Pre-configured token |
| **ha-mcp-oauth** | OAuth HTTP server | Claude.ai, multi-user | OAuth consent form |
| **ha-mcp-sse** | SSE server | Legacy SSE clients | Pre-configured token |

## Requirements

- Docker or Docker Desktop installed
- Network access to Home Assistant
- For OAuth mode: HTTPS endpoint (use Cloudflare Tunnel or similar)

## Troubleshooting

Having issues? Check the [FAQ & Troubleshooting](/faq#ssl-certificates) page.
