# Interesting: mcp-remote Proxy

## Discovery
Many MCP clients that only support stdio transport can access HTTP servers via the `mcp-remote` npm package.

## Usage
```json
{
  "mcpServers": {
    "remote-server": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://server.com/api/mcp"]
    }
  }
}
```

## Use Case
- Claude Desktop (legacy) doesn't natively support HTTP
- Kiro doesn't natively support HTTP
- Any stdio-only client can use this

## Relevance for ha-mcp
If we want to support clients that don't have native HTTP support, we can recommend users use mcp-remote as a bridge.
