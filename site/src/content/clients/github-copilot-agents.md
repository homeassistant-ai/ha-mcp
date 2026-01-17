---
name: GitHub Copilot Agents
company: GitHub
logo: /logos/github.svg
transports: ['stdio', 'sse']
configFormat: json
configLocation: |
  Repository: .vscode/mcp.json (in repo root)
  VS Code: Settings → Extensions → GitHub Copilot → MCP
accuracy: 4
order: 5
httpNote: Requires GitHub Copilot extension and coding agent mode
---

## Configuration

GitHub Copilot Agents supports MCP servers through VS Code integration. Configuration can be repository-specific or personal.

### Repository-Specific Configuration (.vscode/mcp.json)

Create `.vscode/mcp.json` in your repository root:

#### stdio Configuration (Local)

```json
{
  "servers": {
    "home-assistant": {
      "command": "uvx",
      "args": ["ha-mcp@latest"],
      "env": {
        "HOMEASSISTANT_URL": "{{HOMEASSISTANT_URL}}",
        "HOMEASSISTANT_TOKEN": "{{HOMEASSISTANT_TOKEN}}"
      }
    }
  }
}
```

#### With Input Prompts (Secure)

```json
{
  "inputs": [
    {
      "type": "promptString",
      "id": "ha-url",
      "description": "Home Assistant URL",
      "default": "http://homeassistant.local:8123"
    },
    {
      "type": "promptString",
      "id": "ha-token",
      "description": "Long-lived access token",
      "password": true
    }
  ],
  "servers": {
    "home-assistant": {
      "command": "uvx",
      "args": ["ha-mcp@latest"],
      "env": {
        "HOMEASSISTANT_URL": "${input:ha-url}",
        "HOMEASSISTANT_TOKEN": "${input:ha-token}"
      }
    }
  }
}
```

#### SSE Configuration (Network/Remote)

```json
{
  "servers": {
    "home-assistant": {
      "url": "{{MCP_SERVER_URL}}",
      "transport": "sse"
    }
  }
}
```

### Personal Configuration (VS Code settings.json)

For personal configuration in VS Code settings.json, wrap the entire configuration in an `"mcp"` key:

#### stdio Configuration (Local)

```json
{
  "mcp": {
    "servers": {
      "home-assistant": {
        "command": "uvx",
        "args": ["ha-mcp@latest"],
        "env": {
          "HOMEASSISTANT_URL": "{{HOMEASSISTANT_URL}}",
          "HOMEASSISTANT_TOKEN": "{{HOMEASSISTANT_TOKEN}}"
        }
      }
    }
  }
}
```

#### SSE Configuration (Network/Remote)

```json
{
  "mcp": {
    "servers": {
      "home-assistant": {
        "url": "{{MCP_SERVER_URL}}",
        "transport": "sse"
      }
    }
  }
}
```

### Repository Admin Configuration (GitHub UI)

Repository administrators can configure MCP servers directly in GitHub:

1. Navigate to your repository on GitHub
2. Go to **Settings** → **Copilot** → **Coding agent**
3. Scroll to **MCP configuration** section
4. Add the MCP server configuration in JSON format (same as `.vscode/mcp.json`)
5. Save the configuration

This configuration applies to all users with Copilot access to the repository.

## Setup Steps

1. **Enable GitHub Copilot Agent Mode**
   - Open VS Code with GitHub Copilot extension installed
   - Open Copilot Chat (icon in title bar)
   - Select "Agent" from the mode dropdown

2. **Configure MCP Server**
   - Repository admins: Go to repository Settings → Copilot → Coding agent → MCP configuration
   - Personal: Add config to `.vscode/mcp.json` or VS Code settings.json

3. **Start Server Discovery**
   - Click the tools icon in Copilot Chat
   - Click "Start" to initiate server discovery
   - Tools will be cached for future sessions

4. **Verify Connection**
   - In Copilot Agent mode, check available tools
   - GitHub Copilot will use MCP tools autonomously

## Notes

- Requires GitHub Copilot extension in VS Code
- MCP servers policy must be enabled by organization/enterprise admins (disabled by default)
- Copilot coding agent uses tools autonomously without asking for approval
- Configuration format matches VS Code MCP structure
- Repository-level config applies to all users with access to that repository
- Personal config is user-specific
