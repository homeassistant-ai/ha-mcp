---
name: Open WebUI
company: Open WebUI
logo: /logos/open-webui.svg
transports: ['streamable-http']
configFormat: ui
accuracy: 4
order: 14
httpNote: Requires Streamable HTTP - local or remote server
---

## Configuration

Open WebUI natively supports MCP servers via Streamable HTTP transport.

**Requirements:**
- Open WebUI v0.6.31+
- MCP server running in HTTP mode
- **LLM with tool/function calling support** (see [Model Compatibility](#model-compatibility) below)

### Setup Steps

> **Important:** You must use the **Admin Panel** settings, not the user-level "External Tools" option. The Admin Panel is required for MCP server configuration.

1. Navigate to **Admin Panel** → **Settings** → **Tools**
   - Access Admin Panel via the gear icon (you need admin privileges)
   - **Do NOT** use "External Tools" in user settings — that's for a different feature
2. Click **Manage Tool Servers**
3. Click **+ (Add Server)**
4. Enter:
   - **Server URL:** `{{MCP_SERVER_URL}}`
   - **Auth:** Select "None" (or configure if using authentication)
   - **ID** and **Name:** Fill in as desired
5. Click **Save**

### Finding Your MCP URL

**Home Assistant Add-on:** Check the add-on logs for the URL (e.g., `http://homeassistant.local:8086/private_xyz`)

**Docker on same host:** Use `http://host.docker.internal:8086/mcp`

**Local network:** Use `http://192.168.1.100:8086/mcp`

**Remote (HTTPS):** Use `https://your-tunnel.trycloudflare.com/secret_abc123`

### Running Open WebUI

```bash
docker run -d \
  -p 3000:8080 \
  -v open-webui:/app/backend/data \
  --name open-webui \
  ghcr.io/open-webui/open-webui:main
```

Access at: `http://localhost:3000`

## Supported Transports

- **Streamable HTTP** - Native support (recommended)

## Model Compatibility

**Your LLM must support tool/function calling** for MCP tools to work. This is a fundamental requirement — without it, the model cannot invoke Home Assistant tools.

### Recommended Models

**API-based models** (most reliable):
- **Claude** (Anthropic) - Excellent tool calling support
- **GPT-4/GPT-4o** (OpenAI) - Strong tool calling support
- **Gemini** (Google) - Good tool calling support

These models have robust, well-tested tool calling implementations.

### Local Models (Ollama)

> **Warning:** Small local models often struggle with tool calling. If tools aren't being invoked, try an API-based model first to confirm your setup is correct.

Local model considerations:
- **Larger models work better** - 7B+ parameter models have more reliable tool calling
- **Some models don't support tools at all** - Check model documentation
- **Tool calling quality varies** - Even supported models may invoke tools incorrectly
- **Recommended local models**: Llama 3.1 (8B+), Mistral (7B+), Qwen 2.5 (7B+)

If you're using Ollama and tools aren't working:
1. First, test with an API-based model (Claude, GPT-4) to confirm your MCP setup is correct
2. If API models work but Ollama doesn't, the issue is model capability, not configuration
3. Try a larger or different local model with better tool calling support

## Troubleshooting

### Tools Not Appearing or Not Being Called

1. **Check Admin Panel location** - Make sure you added the server in **Admin Panel** → **Settings** → **Tools**, not in user-level "External Tools"
2. **Verify MCP server is running** - Check that ha-mcp is running and accessible at the URL you configured
3. **Test with API model first** - Use Claude or GPT-4 to confirm setup before testing with local models
4. **Check Open WebUI logs** - Look for connection errors or tool registration issues

### "Connection Refused" or Network Errors

1. **Verify the URL is correct** - See [Finding Your MCP URL](#finding-your-mcp-url) above
2. **Check Docker networking**:
   - **ha-mcp on host, Open WebUI in Docker**: Use `http://host.docker.internal:8086/mcp`
   - **Both in Docker on same network**: Use container names (e.g., `http://ha-mcp:8086/mcp`)
3. **Firewall/port issues** - Ensure port 8086 (default) is accessible

### Tools Appear But Model Doesn't Use Them

1. **Model doesn't support tool calling** - Try an API-based model to confirm
2. **Model is too small** - Local models under 7B often fail at tool calling
3. **Prompt the model explicitly** - Try asking "Use the Home Assistant tools to turn on the living room lights"

### Common Mistakes

| Mistake | Solution |
|---------|----------|
| Using "External Tools" instead of Admin Panel | Go to **Admin Panel** → **Settings** → **Tools** |
| Using stdio MCP server URL | ha-mcp must be running in HTTP mode (`ha-mcp-web`) |
| Expecting small local models to work | Start with API models to verify setup, then test local |
| Wrong Docker network address | Use `host.docker.internal` for host access, or container names on a shared Docker network |

## Notes

- Web-based configuration only (no config file)
- Supports both HTTP (local) and HTTPS (remote) URLs
- Use [mcpo](https://github.com/open-webui/mcpo) proxy for stdio-based MCP servers
- See [Open WebUI MCP docs](https://docs.openwebui.com/features/mcp/) for details
