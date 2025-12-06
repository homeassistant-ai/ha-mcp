# GitHub Copilot (GitHub/Microsoft)

## Overview
GitHub Copilot supports MCP servers across multiple IDEs including VS Code, JetBrains, Visual Studio, Eclipse, and Xcode.

---

## Source: MicrosoftDocs/mcp

### Settings Configuration
Navigate to Settings → Coding agent:
```json
{
  "server-name": {
    "type": "http",
    "url": "https://your-server.com/api/mcp",
    "tools": ["*"]
  }
}
```

---

## Source: playwright-mcp

### Interactive CLI
```bash
/mcp add
```

### Configuration File
Edit `~/.copilot/mcp-config.json`:
```json
{
  "mcpServers": {
    "server-name": {
      "type": "local",
      "command": "npx",
      "tools": ["*"],
      "args": ["@package/mcp-server"]
    }
  }
}
```

---

## Source: github-mcp-server

### Docker via Copilot (JetBrains, VS, Eclipse, Xcode)
Location: IDE MCP settings
```json
{
  "mcp": {
    "inputs": [
      {
        "type": "promptString",
        "id": "github_token",
        "description": "GitHub Personal Access Token",
        "password": true
      }
    ],
    "servers": {
      "github": {
        "command": "docker",
        "args": [
          "run", "-i", "--rm", "-e",
          "GITHUB_PERSONAL_ACCESS_TOKEN",
          "ghcr.io/github/github-mcp-server"
        ],
        "env": {
          "GITHUB_PERSONAL_ACCESS_TOKEN": "${input:github_token}"
        }
      }
    }
  }
}
```

---

## Source: Multiple repos (general pattern)

### HTTP Configuration
```json
{
  "server-name": {
    "type": "http",
    "url": "https://your-server.com/api/mcp",
    "tools": ["*"]
  }
}
```

### Local Configuration
```json
{
  "mcpServers": {
    "server-name": {
      "type": "local",
      "command": "npx",
      "args": ["@package/mcp-server"],
      "tools": ["*"]
    }
  }
}
```

---

## Notes
- Available across multiple IDEs (VS Code, JetBrains, Visual Studio, Eclipse, Xcode)
- Uses `tools` field to specify which tools are available
- Supports input prompts for secure credential entry
- Configuration location varies by IDE
