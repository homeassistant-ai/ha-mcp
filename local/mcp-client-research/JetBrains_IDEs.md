# JetBrains IDEs (JetBrains)

## Overview
JetBrains IDEs (IntelliJ IDEA, PyCharm, WebStorm, etc.) support MCP servers via AI Assistant configuration.

---

## Source: github-mcp-server

### Docker Configuration (via Copilot/AI Assistant)
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

## Source: Multiple repos

### Configuration via AI Assistant
1. Open IDE Settings/Preferences
2. Navigate to AI Assistant settings
3. Find MCP configuration section
4. Add server configuration

### Standard Configuration Format
```json
{
  "mcp": {
    "servers": {
      "server-name": {
        "command": "npx",
        "args": ["@package/mcp-server"],
        "env": {
          "API_KEY": "your-key"
        }
      }
    }
  }
}
```

---

## Supported IDEs
- IntelliJ IDEA
- PyCharm
- WebStorm
- PhpStorm
- RubyMine
- GoLand
- Rider
- CLion

---

## Notes
- Requires JetBrains AI Assistant plugin
- Configuration via IDE settings, not separate file
- Supports input prompts for sensitive data
- Works with GitHub Copilot integration
