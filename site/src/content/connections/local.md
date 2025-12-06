---
name: Local Machine
transport: stdio
description: Run ha-mcp on the same computer as your AI client
icon: computer
order: 1
---

## How It Works

Your AI client (Claude Desktop, Cursor, etc.) spawns ha-mcp as a subprocess on your machine. Communication happens via stdin/stdout (stdio).

```
┌─────────────────┐     stdio      ┌─────────────┐
│   AI Client     │ ←────────────→ │   ha-mcp    │
│ (Claude Desktop)│                │  (subprocess)│
└─────────────────┘                └─────────────┘
                                          │
                                          │ HTTP
                                          ↓
                                   ┌─────────────┐
                                   │Home Assistant│
                                   └─────────────┘
```

## Requirements

- **uv** installed on your machine
- AI client that supports stdio transport
- Network access from your machine to Home Assistant

## Best For

- Single-user setups
- Development and testing
- When AI client and HA are on the same network
- Maximum simplicity

## Supported Clients

All clients except ChatGPT and Gemini CLI support stdio:
- Claude Desktop
- Claude Code
- Cursor
- VS Code
- Windsurf
- Cline
- JetBrains IDEs
- Zed
- Continue
- Raycast
