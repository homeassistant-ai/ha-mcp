---
name: ChatGPT
company: OpenAI
logo: /logos/openai.svg
transports: ['sse', 'streamable-http']
configFormat: ui
accuracy: 4
order: 7
beta: true
---

## Configuration

ChatGPT supports MCP servers via the Connectors feature (web UI only).

**Requirements:**
- HTTPS URL (HTTP not supported)
- ChatGPT Plus, Pro, Business, or Enterprise subscription
- Developer Mode enabled

### Setup Steps

1. Open [ChatGPT](https://chatgpt.com)
2. Go to **Settings** → **Connectors**
3. Scroll to **Advanced settings**
4. Enable **Developer mode (beta)** toggle
5. Click **Create** to add a custom connector
6. Enter:
   - **Connector name:** Home Assistant
   - **Endpoint:** `{{MCP_SERVER_URL}}` (must be HTTPS)
   - **Authentication:** None (or OAuth if configured)
7. Click **Create**

### Using in Chat

1. Start a new chat
2. Click the **+** button → **More** → **Developer Mode**
3. Enable your Home Assistant connector
4. The connector is now available for that conversation

### Important

- ChatGPT **only supports HTTPS** - you cannot use HTTP URLs
- MCP endpoint must be at the root path "/"
- Server must accept UUID-format client IDs
- For local development, use tunneling tools (ngrok, Cloudflare Tunnel)

## Supported Transports

- **SSE (Server-Sent Events)** - Supported
- **Streamable HTTP** - Supported

## Notes

- Web-based configuration only (no config file)
- Requires HTTPS endpoint
- Requires paid subscription (Plus/Pro/Business/Enterprise)
- No stdio support - remote server required
- This feature is currently in **BETA**

## UX Limitation

ChatGPT's MCP support requires navigating to Settings → Connectors each time you want to enable it for a conversation. This can be cumbersome for frequent use.

**Alternative:** If you have a Plus subscription, consider using **GitHub Copilot Codex** in VS Code instead - it uses the same MCP configuration as VS Code Copilot and provides a better integrated experience for agentic workflows.
