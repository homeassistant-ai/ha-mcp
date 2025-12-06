---
name: Home Assistant Add-on
description: Run ha-mcp inside Home Assistant OS
icon: home-assistant
forConnections: ['network', 'remote']
order: 3
---

## Overview

The easiest way to run ha-mcp if you use Home Assistant OS. The add-on:
- Auto-discovers Home Assistant connection
- Generates secure secret path automatically
- No token configuration needed

## Installation

1. **Add the repository:**

   [![Add Repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fhomeassistant-ai%2Fha-mcp)

   Or manually add: `https://github.com/homeassistant-ai/ha-mcp`

2. **Install** "Home Assistant MCP Server" from the add-on store

3. **Start** the add-on

4. **Check the logs** for your MCP server URL:

   ```
   MCP Server URL: http://192.168.1.100:9583/private_zctpwlX7ZkIAr7oqdfLPxw
   ```

## Using the Add-on URL

Configure your AI client with the URL from the logs.

**For Claude Desktop** (requires mcp-proxy):

```json
{
  "mcpServers": {
    "home-assistant": {
      "command": "mcp-proxy",
      "args": ["--transport", "streamablehttp", "http://192.168.1.100:9583/private_xxx"]
    }
  }
}
```

**For Claude Code:**

```bash
claude mcp add --transport http home-assistant http://192.168.1.100:9583/private_xxx
```

## Features

- **Zero configuration** - Auto-connects to Home Assistant
- **Secure by default** - 128-bit random secret path
- **Persistent** - Secret path saved across restarts
- **Port 9583** - Default listening port
