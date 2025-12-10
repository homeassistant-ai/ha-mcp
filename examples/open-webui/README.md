# Open WebUI + Ollama + ha-mcp Demo

A complete local AI stack for controlling Home Assistant with natural language.

## What's Included

- **Open WebUI** - ChatGPT-like interface (port 3000)
- **Ollama** - Local LLM runtime (port 11434)
- **smollm2:1.7b** - Small model with tool calling (~1GB RAM)
- **ha-mcp** - Home Assistant MCP server (pre-configured)

## Quick Start

1. **Configure Home Assistant credentials:**
   ```bash
   cp .env.example .env
   # Edit .env with your HA URL and token
   ```

2. **Start the stack:**
   ```bash
   docker compose up -d
   ```

3. **Wait for model download** (first time only, ~1GB):
   ```bash
   docker compose logs -f ollama-init
   ```

4. **Open the UI:**
   - URL: http://localhost:3000
   - No login required (demo mode)

5. **Test it:**
   > "What lights are on in my house?"
   > "Turn off the living room lights"

## Requirements

- Docker & Docker Compose
- ~2GB RAM for the model
- Home Assistant with a long-lived access token

## Configuration

### Using a different model

Edit `docker-compose.yml`:
- Change `smollm2:1.7b` in both `ollama-init` and `DEFAULT_MODELS`
- Other small models with tool support:
  - `qwen2.5:1.5b` (~1GB)
  - `granite3.1-moe:1b` (~1GB)
  - `granite4:2b` (~1.5GB, better tool calling)

### Enable authentication

Remove or change `WEBUI_AUTH=false` in the open-webui service.

### GPU acceleration (NVIDIA)

Uncomment the `deploy` section in the ollama service.

## Troubleshooting

**Model not loading?**
```bash
docker compose logs ollama-init
```

**MCP connection issues?**
```bash
docker compose logs ha-mcp
# Test connectivity:
docker compose exec open-webui curl http://ha-mcp:8086/mcp
```

**Reset everything:**
```bash
docker compose down -v
docker compose up -d
```
