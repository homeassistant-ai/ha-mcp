# MCP Tool Calling Testing Stack

Test MCP tool calling with local LLMs using Open WebUI or LibreChat.

This stack helps diagnose tool calling issues with local models by providing:
- **mini-mcp**: A minimal MCP server with 3 simple tools (get_time, add_numbers, greet)
- **ha-mcp**: Full Home Assistant MCP server
- **MCPO**: MCP-to-OpenAPI bridge (helps with compatibility)
- Two frontends: Open WebUI and LibreChat

## Test Strategy

1. **Test mini-mcp first** - If the model can't call `get_time()`, it won't work with ha-mcp
2. **Test via MCPO** - OpenAPI endpoints work better with some models
3. **Test ha-mcp** - After confirming tools work with simple server

## Quick Start (Remote Deployment)

```bash
# Configure your HA credentials
cp .env.example .env
# Edit .env with your settings

# Deploy to remote server with Docker
./deploy.sh user@your-server.com          # Open WebUI (default)
./deploy.sh user@your-server.com librechat # LibreChat
```

## Local Testing

```bash
cp .env.example .env
# Edit .env

# Open WebUI stack
docker compose up -d

# OR LibreChat stack
docker compose -f docker-compose.librechat.yml up -d
```

## Endpoints

| Service | Port | Description |
|---------|------|-------------|
| Open WebUI | 3000 | Chat interface |
| LibreChat | 3080 | Alternative chat interface |
| MCPO mini-mcp | 8001 | OpenAPI docs: `/mini-mcp/docs` |
| MCPO ha-mcp | 8000 | OpenAPI docs: `/ha-mcp/docs` |
| ha-mcp (native) | 8086 | MCP endpoint: `/mcp` |
| Ollama | 11434 | LLM API |

## Test Prompts

**Mini MCP (test these first):**
- "What time is it?" → calls `get_time()`
- "What is 5 + 7?" → calls `add_numbers(5, 7)`
- "Say hello to Bob" → calls `greet("Bob")`

**Home Assistant MCP:**
- "What lights are on?" → calls entity search
- "Turn off the living room" → calls device control

## Adding Tool Servers in Open WebUI

Go to **Admin Panel → Settings → Tools → Add Connection**

| Setting | Value |
|---------|-------|
| Type | OpenAPI |
| URL (mini-mcp) | `http://mcpo-mini:8000/mini-mcp` |
| URL (ha-mcp) | `http://mcpo-ha:8000/ha-mcp` |
| Auth | None |

## Limiting ha-mcp Tools

For smaller models, reduce the number of tools:

```bash
# In .env
ENABLED_TOOL_MODULES=tools_config_automations,tools_search
```

This exposes only ~7 tools instead of 80+.

## Models with Tool Calling Support

| Model | Size | Tool Calling Quality |
|-------|------|---------------------|
| qwen2.5:7b | ~4GB | Good |
| llama3.1:8b | ~5GB | Good |
| qwen2.5:1.5b | ~1GB | Basic |
| granite4:2b | ~1.5GB | Decent |

## Troubleshooting

**Check if mini-mcp is working:**
```bash
curl http://localhost:8001/mini-mcp/docs
```

**Check ha-mcp logs:**
```bash
docker compose logs -f ha-mcp
docker compose logs -f mcpo-ha
```

**Test Ollama tool calling directly:**
```bash
curl http://localhost:11434/api/chat -d '{
  "model": "qwen2.5:7b",
  "messages": [{"role": "user", "content": "What time is it?"}],
  "tools": [{
    "type": "function",
    "function": {
      "name": "get_time",
      "description": "Get current time",
      "parameters": {"type": "object", "properties": {}}
    }
  }],
  "stream": false
}'
```

**Reset everything:**
```bash
docker compose down -v
docker compose up -d
```

## Files

- `docker-compose.yml` - Open WebUI stack
- `docker-compose.librechat.yml` - LibreChat stack
- `librechat.yaml` - LibreChat MCP configuration
- `mini_mcp.py` - Minimal MCP server for testing
- `deploy.sh` - Remote deployment script
