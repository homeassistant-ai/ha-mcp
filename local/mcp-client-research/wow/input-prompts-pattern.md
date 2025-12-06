# Interesting: Input Prompts for Credentials

## Discovery
VS Code and GitHub Copilot support input prompts for secure credential entry.

## Pattern
```json
{
  "inputs": [
    {
      "type": "promptString",
      "id": "api_key",
      "description": "Enter your API Key",
      "password": true
    }
  ],
  "servers": {
    "my-server": {
      "env": {
        "API_KEY": "${input:api_key}"
      }
    }
  }
}
```

## Benefits
- Credentials not stored in plain text
- Prompt shown on server start
- `password: true` masks input
- More secure than hardcoded values

## Relevance for ha-mcp
For VS Code users, we could document this pattern for Home Assistant tokens:
```json
{
  "inputs": [
    {
      "type": "promptString",
      "id": "ha_token",
      "description": "Home Assistant Long-Lived Access Token",
      "password": true
    }
  ],
  "servers": {
    "home-assistant": {
      "command": "uvx",
      "args": ["ha-mcp@latest"],
      "env": {
        "HOMEASSISTANT_TOKEN": "${input:ha_token}"
      }
    }
  }
}
```
