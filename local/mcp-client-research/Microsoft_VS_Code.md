# VS Code (Microsoft)

## Overview
Visual Studio Code supports MCP servers via settings.json or workspace configuration files.

## Configuration Locations
- **User Settings**: `settings.json` (use `mcp.servers` key)
- **Workspace**: `.vscode/mcp.json` (use `servers` key)

---

## Source: awesome-remote-mcp-servers

### HTTP Remote Configuration
Add to `settings.json`:
```json
{
  "mcp": {
    "servers": {
      "server-name": {
        "type": "http",
        "url": "https://your-server.com/sse"
      }
    }
  }
}
```

---

## Source: MicrosoftDocs/mcp

### One-Click Installation
- Use the `@mcp` search in Extensions marketplace
- Click installation badges from MCP server documentation

### Manual via Settings
Add to user `settings.json`:
```json
{
  "mcp.servers": {
    "server-name": {
      "type": "http",
      "url": "https://your-server.com/api/mcp"
    }
  }
}
```

---

## Source: playwright-mcp

### STDIO Configuration via CLI
```bash
code --add-mcp '{"name":"playwright","command":"npx","args":["@playwright/mcp@latest"]}'
```

### Via Settings
Settings → MCP configuration section

---

## Source: github-mcp-server

### Remote with OAuth (v1.101+)
```json
{
  "servers": {
    "github": {
      "type": "http",
      "url": "https://api.githubcopilot.com/mcp/"
    }
  }
}
```

### Remote with PAT
```json
{
  "servers": {
    "github": {
      "type": "http",
      "url": "https://api.githubcopilot.com/mcp/",
      "headers": {
        "Authorization": "Bearer ${input:github_mcp_pat}"
      }
    }
  },
  "inputs": [
    {
      "type": "promptString",
      "id": "github_mcp_pat",
      "description": "GitHub Personal Access Token",
      "password": true
    }
  ]
}
```

### Workspace Configuration (.vscode/mcp.json)
```json
{
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
```

---

## Source: Multiple repos (general pattern)

### User Settings Format
```json
{
  "mcp": {
    "servers": {
      "server-name": {
        "type": "stdio",
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

### Workspace Format (.vscode/mcp.json)
```json
{
  "servers": {
    "server-name": {
      "command": "npx",
      "args": ["@package/mcp-server"]
    }
  }
}
```

### Input Prompts for Sensitive Data
```json
{
  "inputs": [
    {
      "type": "promptString",
      "id": "api_key",
      "description": "Enter API Key",
      "password": true
    }
  ],
  "servers": {
    "my-server": {
      "command": "npx",
      "args": ["@package/server"],
      "env": {
        "API_KEY": "${input:api_key}"
      }
    }
  }
}
```

---

## Commands
- `/mcp: List Servers` - List configured MCP servers
- `/mcp: Start server` - Start a specific server

---

## Notes
- VS Code supports input prompts for secure credential entry
- Workspace config overrides user settings
- Supports both stdio and HTTP transports
- `type` field required: `"stdio"` or `"http"`
