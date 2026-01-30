# AWS Cognito OAuth for ha-mcp (Beta)

This deployment mode runs ha-mcp as a remote HTTP MCP server protected by **AWS Cognito OAuth**.

It’s useful when you want:
- A public HTTPS endpoint (e.g. on AWS App Runner)
- Single Sign-On / user management via Cognito
- Claude MCP connector compatibility (PKCE public clients)

## Start the Cognito Server

### Docker

```bash
docker run -d --name ha-mcp-cognito \
  -p 8086:8086 \
  -e HOMEASSISTANT_URL=http://homeassistant.local:8123 \
  -e HOMEASSISTANT_TOKEN=your_token \
  -e MCP_BASE_URL=https://your-domain.example \
  -e COGNITO_USER_POOL_ID=us-east-1_XXXXXX \
  -e COGNITO_CLIENT_ID=xxxx \
  -e COGNITO_CLIENT_SECRET=xxxx \
  ghcr.io/homeassistant-ai/ha-mcp:latest \
  ha-mcp-cognito
```

### uvx

```bash
MCP_BASE_URL=https://your-domain.example \
COGNITO_USER_POOL_ID=us-east-1_XXXXXX \
COGNITO_CLIENT_ID=xxxx \
COGNITO_CLIENT_SECRET=xxxx \
HOMEASSISTANT_URL=http://homeassistant.local:8123 \
HOMEASSISTANT_TOKEN=your_token \
uvx ha-mcp@latest ha-mcp-cognito
```

## Environment Variables

### Required

| Variable | Description |
|---|---|
| `HOMEASSISTANT_URL` | Home Assistant base URL |
| `HOMEASSISTANT_TOKEN` | Long-lived access token |
| `COGNITO_USER_POOL_ID` | Cognito User Pool ID (e.g. `us-east-1_XXXX`) |
| `COGNITO_CLIENT_ID` | Cognito App Client ID |
| `COGNITO_CLIENT_SECRET` | Cognito App Client Secret |

### Recommended

| Variable | Description |
|---|---|
| `MCP_BASE_URL` | Public URL of this service **without** `/mcp` |

### Optional

| Variable | Description | Default |
|---|---|---|
| `MCP_PORT` | Server port | `8086` |
| `MCP_SECRET_PATH` | MCP endpoint path | `/mcp` |
| `AWS_REGION` | Cognito region (inferred from `COGNITO_USER_POOL_ID` when possible) | *(inferred)* |
| `COGNITO_REDIRECT_PATH` | OAuth callback path used by FastMCP | `/auth/callback` |
| `COGNITO_ALLOWED_CLIENT_REDIRECT_URIS` | Comma-separated allowlist patterns | *(Claude + localhost)* |

## Health Check

This mode exposes `GET /healthz` for container platforms.

## Claude Connector URL

Use:
- MCP endpoint: `https://your-domain.example/mcp`

> If you see a 404 during OAuth discovery, double-check `MCP_BASE_URL` does **not** include `/mcp`.

