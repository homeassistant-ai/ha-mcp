---
name: Local Network
transport: http
description: Run ha-mcp as an HTTP server accessible on your LAN
icon: network
order: 2
---

## How It Works

ha-mcp runs as a persistent HTTP server on your network. AI clients connect to it via HTTP URL.

```
┌─────────────────┐                ┌─────────────────┐
│   AI Client     │                │   AI Client     │
│   (Desktop)     │                │   (Laptop)      │
└────────┬────────┘                └────────┬────────┘
         │                                  │
         └──────────┐    HTTP    ┌──────────┘
                    ↓            ↓
              ┌─────────────────────┐
              │      ha-mcp         │
              │   HTTP Server       │
              │ (192.168.1.x:8086)  │
              └──────────┬──────────┘
                         │
                         ↓
              ┌─────────────────────┐
              │   Home Assistant    │
              └─────────────────────┘
```

## Requirements

- ha-mcp running as HTTP server (uvx, Docker, or HA Add-on)
- AI client with HTTP transport support
- All devices on the same network

## Best For

- Multiple clients accessing the same server
- Clients that don't support stdio (ChatGPT, Gemini CLI)
- Centralized MCP server management
- Running server on always-on device (NAS, Raspberry Pi)

## Deployment Options

1. **uvx** - Run `ha-mcp-web` command
2. **Docker** - Container with HTTP mode
3. **Home Assistant Add-on** - Easiest for HA OS users

## Supported Clients

All clients support HTTP transport:
- Claude Desktop (via mcp-proxy)
- Claude Code
- Cursor
- VS Code
- Windsurf
- Cline
- ChatGPT (requires HTTPS)
- Gemini CLI
