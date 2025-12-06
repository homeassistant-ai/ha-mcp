---
name: VS Code
company: Microsoft
logo: /logos/vscode.svg
transports: ['stdio', 'sse', 'streamable-http']
configFormat: json
configLocation: Settings → Extensions → GitHub Copilot → MCP
accuracy: 5
order: 4
---

## Configuration

VS Code with GitHub Copilot supports MCP servers via settings.json or the UI.

### One-Click Install

[![Install in VSCode](https://img.shields.io/badge/VSCode-Install_Home_Assistant_MCP-0098FF?style=flat-square&logo=visualstudiocode&logoColor=ffffff)](vscode:mcp/install?%7B%22name%22%3A%22Home%20Assistant%22%2C%22inputs%22%3A%5B%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22homeassistant-url%22%2C%22description%22%3A%22Your%20Home%20Assistant%20URL%20(ex%3A%20http%3A%2F%2Fhomeassistant.local%3A8123)%22%2C%22default%22%3A%22http%3A%2F%2Fhomeassistant.local%3A8123%22%2C%22password%22%3Afalse%7D%2C%7B%22type%22%3A%22promptString%22%2C%22id%22%3A%22homeassistant-token%22%2C%22description%22%3A%22Your%20long%20lived%20access%20token%20(generate%20in%20your%20profile%20page%2C%20Security%20tab)%22%2C%22password%22%3Atrue%7D%5D%2C%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22ha-mcp%40latest%22%5D%2C%22env%22%3A%7B%22HOMEASSISTANT_URL%22%3A%22%24%7Binput%3Ahomeassistant-url%7D%22%2C%22HOMEASSISTANT_TOKEN%22%3A%22%24%7Binput%3Ahomeassistant-token%7D%22%7D%7D)

### stdio Configuration (Local)

Add to your `settings.json`:

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

### With Input Prompts (Secure)

VS Code supports secure credential prompts:

```json
{
  "mcp": {
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
}
```

### SSE Configuration (Network/Remote)

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

### Streamable HTTP Configuration (Network/Remote)

```json
{
  "mcp": {
    "servers": {
      "home-assistant": {
        "url": "{{MCP_SERVER_URL}}",
        "transport": "http"
      }
    }
  }
}
```

## Notes

- Requires GitHub Copilot extension
- Supports secure input prompts for credentials
- Uses `mcp.servers` (not `mcpServers`)
- Reload VS Code after config changes
