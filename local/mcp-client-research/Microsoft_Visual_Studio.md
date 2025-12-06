# Visual Studio (Microsoft)

## Overview
Visual Studio 2022/2026 has built-in MCP support.

---

## Source: MicrosoftDocs/mcp

### Built-in Support
"Microsoft Learn" MCP is built-in with VS 2022 or 2026; no configuration needed.

For custom servers, configure via IDE MCP settings.

---

## Source: github-mcp-server

### Docker Configuration (via Copilot)
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

## Notes
- Built-in MCP support in VS 2022 and VS 2026
- Some MCP servers pre-installed
- Configuration similar to VS Code but via IDE settings
- Supports GitHub Copilot integration
