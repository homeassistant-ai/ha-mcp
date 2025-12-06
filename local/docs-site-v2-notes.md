# Docs Site V2 - Implementation Notes (Updated)

## New Wizard Flow (Vertical Accumulation)

All sections remain visible as user progresses. User can click any previous selection to change it.

### Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│  1. SELECT CLIENT                                                   │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐               │
│  │⚡ Claude │ │⚡ Cursor │ │🔧ChatGPT │ │🔧Claude.ai│ ...          │
│  │ Desktop  │ │          │ │          │ │           │               │
│  └──────────┘ └──────────┘ └──────────┘ └───────────┘               │
│  [Selected: Claude Desktop]                                         │
└─────────────────────────────────────────────────────────────────────┘
                              ↓ (appears below)
┌─────────────────────────────────────────────────────────────────────┐
│  2. SELECT CONNECTION TYPE                                          │
│  ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐       │
│  │ ⚡ Local Machine │ │ 🔧 Local Network │ │ 🛠️ Remote      │       │
│  │ stdio - direct  │ │ HTTP via proxy  │ │ HTTPS + proxy  │       │
│  └─────────────────┘ └─────────────────┘ └─────────────────┘       │
│  [Selected: Local Network]                                          │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────┐
│  3. SELECT PLATFORM (for uvx prerequisite)                          │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐                            │
│  │  macOS   │ │  Linux   │ │ Windows  │                            │
│  └──────────┘ └──────────┘ └──────────┘                            │
│  [Shows uvx installation for selected platform]                     │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────┐
│  4. MCP SERVER DEPLOYMENT (for Network/Remote)                      │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐                │
│  │ ⚡ uvx        │ │ 🔧 Docker    │ │ 🔧 HA Add-on │                │
│  │ (ha-mcp-web) │ │ container    │ │ (HA OS only) │                │
│  └──────────────┘ └──────────────┘ └──────────────┘                │
└─────────────────────────────────────────────────────────────────────┘
                              ↓ (only for Remote)
┌─────────────────────────────────────────────────────────────────────┐
│  5. REVERSE PROXY (for Remote only)                                 │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐                │
│  │ ⚡ Cloudflare │ │ 🔧 Custom    │ │ 🛠️ HA Custom │                │
│  │ Tunnel       │ │ Reverse Proxy│ │ Component    │                │
│  │              │ │ (Caddy/Nginx)│ │ Coming Soon  │                │
│  └──────────────┘ └──────────────┘ └──────────────┘                │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────┐
│  6. CONFIGURATION OUTPUT                                            │
│  [All instructions + final config based on selections]              │
└─────────────────────────────────────────────────────────────────────┘
```

## Client Categories

### stdio + HTTP (Both)
- Claude Desktop (stdio native, HTTP via mcp-proxy)
- Claude Code (both native)
- Cursor (both native)
- VS Code (both native)
- Windsurf (both native)
- Cline (both native)

### stdio Only
- JetBrains IDEs
- Zed
- Continue.dev
- Raycast

For network/remote: Use mcp-proxy (`uvx mcp-proxy ...`)

### HTTP Only
- ChatGPT (Remote only - requires HTTPS)
- Claude.ai (Remote only - requires HTTPS)
- Gemini CLI (Network + Remote)

## Connection Type Availability

| Client | Local (stdio) | Local Network (HTTP) | Remote (HTTPS) |
|--------|---------------|---------------------|----------------|
| Claude Desktop | ✅ Direct | ✅ via mcp-proxy | ✅ via mcp-proxy |
| Claude Code | ✅ Direct | ✅ Native HTTP | ✅ Native HTTP |
| Cursor | ✅ Direct | ✅ Native HTTP | ✅ Native HTTP |
| VS Code | ✅ Direct | ✅ Native HTTP | ✅ Native HTTP |
| Windsurf | ✅ Direct | ✅ Native HTTP | ✅ Native HTTP |
| Cline | ✅ Direct | ✅ Native HTTP | ✅ Native HTTP |
| ChatGPT | ❌ | ❌ | ✅ HTTPS required |
| Claude.ai | ❌ | ❌ | ✅ HTTPS required |
| Gemini CLI | ❌ | ✅ Native HTTP | ✅ Native HTTP |
| JetBrains | ✅ Direct | ✅ via mcp-proxy | ✅ via mcp-proxy |
| Zed | ✅ Direct | ✅ via mcp-proxy | ✅ via mcp-proxy |
| Continue | ✅ Direct | ✅ via mcp-proxy | ✅ via mcp-proxy |
| Raycast | ✅ Direct | ✅ via mcp-proxy | ✅ via mcp-proxy |

## Complexity Indicators

- **⚡ Quick** - 1-2 steps, minimal config
- **🔧 Complex** - 3-4 steps, some setup required
- **🛠️ Advanced** - 5+ steps, technical knowledge needed

## Prerequisites by Path

### Local Machine (stdio)
1. Install uvx (platform-specific)
2. Configure client

### Local Network (HTTP) with stdio-only client
1. Install uvx (platform-specific)
2. Install mcp-proxy: `uvx mcp-proxy`
3. Deploy MCP server (uvx/docker/addon)
4. Configure client with mcp-proxy

### Local Network (HTTP) with HTTP-native client
1. Deploy MCP server (uvx/docker/addon)
   - If uvx: Install uvx first (platform-specific)
   - If Docker: Install Docker first
2. Configure client

### Remote (HTTPS)
1. Deploy MCP server (uvx/docker/addon)
   - Platform prereqs as above
2. Deploy reverse proxy (cloudflared/custom)
3. Configure client with HTTPS URL

## mcp-proxy Usage

For stdio-only clients accessing HTTP servers:

```bash
# In client config, use mcp-proxy as command
{
  "mcpServers": {
    "home-assistant": {
      "command": "uvx",
      "args": ["mcp-proxy", "--transport", "streamablehttp", "http://SERVER_URL"]
    }
  }
}
```

Note: Using `uvx mcp-proxy` instead of `uv tool install mcp-proxy` for simpler one-command setup.
