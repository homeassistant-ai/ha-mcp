# Interesting: MCP Transport Types

## Discovery
MCP has multiple transport types, and client support varies.

## Transport Types

### 1. STDIO (Standard Input/Output)
- Most widely supported
- Command-based execution
- Local process spawning
- Used by: Claude Desktop, Cursor, VS Code, Windsurf, Cline, etc.

### 2. HTTP/SSE (Server-Sent Events)
- Remote server support
- URL-based configuration
- Persistent connections
- Used by: VS Code, ChatGPT, Gemini CLI, etc.

### 3. Streamable HTTP
- Modern HTTP variant
- Better for streaming responses
- Used by: Cline (with `type: "streamableHttp"`)

## Client Support Matrix

| Client | STDIO | HTTP | SSE | Streamable |
|--------|-------|------|-----|------------|
| Claude Desktop | Yes | Via proxy | Via proxy | No |
| Cursor | Yes | Yes | Yes | ? |
| VS Code | Yes | Yes | Yes | ? |
| Windsurf | Yes | Yes | Yes | ? |
| Cline | Yes | ? | ? | Yes |
| ChatGPT | No | Yes | Yes | ? |

## Relevance for ha-mcp
ha-mcp should support both stdio and HTTP to maximize client compatibility.
