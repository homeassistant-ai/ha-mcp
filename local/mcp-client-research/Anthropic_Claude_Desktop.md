# Claude Desktop (Anthropic)

## Overview
Claude Desktop is Anthropic's official desktop application for Claude AI. It supports MCP servers via JSON configuration.

## Configuration File Location
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

---

## Source: awesome-remote-mcp-servers

### HTTP/Remote Configuration
Navigate to Settings → Connectors → Add Custom Connector:
- Name: `server-name`
- URL: `https://your-mcp-server.com/sse`

*Note: Organization members may lack custom connector access.*

---

## Source: MicrosoftDocs/mcp

### Legacy/Proxy Configuration (for remote servers)
```json
{
  "mcpServers": {
    "server-name": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://your-server.com/api/mcp"]
    }
  }
}
```

---

## Source: playwright-mcp

### Standard STDIO Configuration
```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp@latest"]
    }
  }
}
```

---

## Source: github-mcp-server

### Docker Configuration with PAT
```json
{
  "mcpServers": {
    "github": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm", "-e",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "ghcr.io/github/github-mcp-server"
      ],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "your-token"
      }
    }
  }
}
```

---

## Source: Multiple repos (general pattern)

### Standard Configuration Format
```json
{
  "mcpServers": {
    "server-name": {
      "command": "npx|uvx|python|node",
      "args": ["package-name@version", "additional-args"],
      "env": {
        "API_KEY": "your-api-key",
        "OTHER_VAR": "value"
      }
    }
  }
}
```

### Common Command Types
- `npx` - Node.js packages
- `uvx` - Python packages (via uv)
- `python` - Direct Python scripts
- `node` - Direct Node.js scripts
- `docker` - Containerized servers

---

## Notes
- Restart Claude Desktop after config changes: Claude menu → Quit Claude, then reopen
- Config file must be valid JSON
- Environment variables can be used for sensitive data
- Claude Desktop does NOT inherit shell PATH - use full paths for commands
