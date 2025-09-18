# The Unofficial and Awesome Home Assistant MCP Server

[![Test Suite](https://img.shields.io/badge/Tests-E2E%20%2B%20Integration-brightgreen)](tests/)
[![MCP Version](https://img.shields.io/badge/MCP-1.12.0-blue)](https://modelcontextprotocol.io/)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](https://python.org)
[![FastMCP](https://img.shields.io/badge/FastMCP-2.10.5-orange)](https://github.com/jlowin/fastmcp)

> A comprehensive Model Context Protocol (MCP) server that enables AI assistants to interact with Home Assistant. Using natural language, control smart home devices, query states, execute services and manage your automations.

## ✨ Features

### 🔍 Discover, Search and Query
- **Fuzzy Entity Search**: Comprehensive search with typo tolerance
- **AI-Optimized System Overview**: Complete system analysis showing entity counts, areas, and device status
- **Intelligent Entity Matching**: Advanced search across all Home Assistant entities with partial name matching
- **Template Evaluation**: Evaluate Home Assistant templates for dynamic data processing and calculations
- **Logbook Data Access**: Query logbook entries with date filtering and entity-specific searches

### 🏠 Control
- **Universal Service Control**: Execute any Home Assistant service with full parameter support
- **Real-time State Access**: Get detailed entity states with attributes, timestamps, and context information

### 🔧 Manage
- **Automation and Scripts**: Create, modify, delete, enable/disable, and trigger Home Assistant automations
- **Helper Entity Management**: Create, modify, and delete input_boolean, input_number, input_select, input_text, input_datetime, and input_button entities


## 🚀 Quick Start

### Prerequisites

- **Long-lived access token** from Home Assistant user profile - Security tab

### Installation

1. **Install uv** 

uv is a Python package manager (Python installation not required)
Follow instructions at https://docs.astral.sh/uv/getting-started/installation/

2. **Clone the repository**
   ```bash
   git clone https://github.com/homeassistant-mcp/homeassistant-mcp
   cd homeassistant-mcp
   ```

## Client Configuration

### mcp.json format (Claude Desktop, VSCode, etc.)

Linux/WSL/MacOs:
```json
{
  "mcpServers": {

    "homeassistant-mcp": {
      "command": "path/to/homeassistant-mcp/run_mcp_server.sh",
      "args": [],
      "env": {
        "HOMEASSISTANT_URL": "http://localhost:8123",
        "HOMEASSISTANT_TOKEN": "your_long_lived_access_token_from_home_assitant_profile"
      }
    }

  }
}
```

Windows:
```json
{
  "mcpServers": {

    "homeassistant-mcp": {
      "command": "C:\\path\\to\\homeassistant-mcp\\run_mcp_server.bat",
      "args": [],
      "env": {
        "HOMEASSISTANT_URL": "http://localhost:8123",
        "HOMEASSISTANT_TOKEN": "your_long_lived_access_token_from_home_assitant_profile"
      }
    }

  }
}
```

### Claude code

```bash
cd homeassistant-mcp
uv sync
claude mcp add homeassistant-mcp -- uv --directory /path/to/homeassistant-mcp --env HOMEASSISTANT_URL=http://localhost:8123 --env HOMEASSISTANT_TOKEN=your_token run fastmcp run
claude mcp add-json homeassistant-mcp '{"type":"stdio","command":"uv","args":["--directory","/path/to/homeassistant-mcp","run","fastmcp","run"],"env":{"HOMEASSISTANT_URL":"http://localhost:8123","HOMEASSISTANT_TOKEN":"your_token"}}'
```


### Remote mode (for compatibility with remote mcp)

1. **Configure environment**
   ```bash
   cp .env.example .env
   # Edit .env with your Home Assistant details:
   HOMEASSISTANT_URL=http://localhost:8123
   HOMEASSISTANT_TOKEN=your_token
  ```
2. **Start the server**
```bash
uv run fastmcp run --transport streamable-http --port 8086
```

Server will be avaiable at http://127.0.0.1:8086/mcp


## Online clients (Claude.ai, ChatGPT.com, ...)

> WARNING! This is not the most secure way of connecting those providers. Use this setup at your own risk. Anybody figuring out how to do it properly is welcome to contribute to this project. Check out https://gofastmcp.com/servers/auth/authentication for more information. 

This setup consist of an https tunnel with cloudflared tunnel.

1. **Install cloudflared** See https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/

2. **Configure environment**
   ```bash
   cp .env.example .env
   # Edit .env with your Home Assistant details:
   HOMEASSISTANT_URL=http://localhost:8123
   HOMEASSISTANT_TOKEN=your_token
  ```

3. **Run the MCP server**

```bash
uv run fastmcp run --transport streamable-http --port 8086 --path __my_secret_key_that_should_not_be_shared_with_anyone__
```

> Replace the path parameter with a secret value!

3. **Start the tunnel**

```bash
cloudflared tunnel --url http://localhost:8086
```

You will find the base url in your output. It will look like this: https://abc-def-ghi.trycloudflare.com

Append the your secret path and use the url in the online provider (Claude.ai and such)

The url should look like : https://abc-def-ghi.trycloudflare.com/__my_secret_key_that_should_not_be_shared_with_anyone__

For Claude.AI: https://support.anthropic.com/en/articles/11176164-pre-built-web-connectors-using-remote-mcp
ChatGPT.com: https://help.openai.com/en/articles/11487775-connectors-in-chatgpt (untested)



## 🛠️ Available Tools

### Search & Discovery Tools
| Tool | Description | Example |
|------|-------------|---------|
| `ha_search_entities` | Comprehensive entity search with fuzzy matching | `ha_search_entities("lumiere salon")` |
| `ha_get_overview` | AI-optimized system overview with entity counts | `ha_get_overview()` |

### Core Home Assistant API Tools
| Tool | Description | Example |
|------|-------------|---------|
| `ha_get_state` | Get entity state with attributes and context | `ha_get_state("light.living_room")` |
| `ha_call_service` | Execute any Home Assistant service | `ha_call_service("light", "turn_on", {"entity_id": "light.all"})` |
| `ha_get_services` | List available services by domain | `ha_get_services("light")` |
| `ha_get_config` | Get Home Assistant configuration | `ha_get_config()` |
| `ha_get_events` | List available event types | `ha_get_events()` |
| `ha_fire_event` | Fire custom events | `ha_fire_event("custom_event", {"data": "value"})` |

### Helper Entity Management Tools
| Tool | Description | Example |
|------|-------------|---------|
| `ha_manage_helper` | Create/modify/delete 6 types of helpers | `ha_manage_helper("create", "input_boolean", {"name": "test"})` |
| `ha_get_helpers` | List all helper entities by type | `ha_get_helpers("input_boolean")` |

### Script Management Tools
| Tool | Description | Example |
|------|-------------|---------|
| `ha_manage_script` | Full script lifecycle management | `ha_manage_script("create", "test_script", {"sequence": []})` |
| `ha_get_scripts` | List all available scripts | `ha_get_scripts()` |

### Automation Management Tools
| Tool | Description | Example |
|------|-------------|---------|
| `ha_manage_automation` | Complete automation lifecycle | `ha_manage_automation("create", "test_auto", {"trigger": []})` |
| `ha_get_automations` | List all automations with metadata | `ha_get_automations()` |

### Template & Data Tools
| Tool | Description | Example |
|------|-------------|---------|
| `ha_eval_template` | Evaluate Jinja2 templates | `ha_eval_template("{{ states('sensor.temperature') }}")` |
| `ha_get_logbook` | Access historical logbook entries | `ha_get_logbook("2024-01-01", "light.living_room")` |


## 🤝 Contributing

For development setup, testing instructions, and contribution guidelines, see **[CONTRIBUTING.md](CONTRIBUTING.md)**.

For comprehensive testing documentation, see **[tests/README.md](tests/README.md)**.

## 🛣️ Development Roadmap

### Completed ✅
- [x] Core infrastructure and HTTP client
- [x] FastMCP integration with OpenAPI auto-generation
- [x] Smart search tools with fuzzy matching
- [x] Optimized tool documentation to reduce tool call errors.
- [x] WebSocket async device control
- [x] Convenience tools for scenes and automations
- [x] Comprehensive test suite

### Future Enhancements 🔮
- [ ] Docker image for enhanced security, isolation and distribution
- [ ] Home Assistant addon
- [ ] Home Assistant WebSocket event streaming
- [ ] Integration with [Home Assistant MCP Server](https://www.home-assistant.io/integrations/mcp_server)
- Open an issuer to add more!

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- **[Home Assistant](https://home-assistant.io/)**: Amazing smart home platform (!)
- **[FastMCP](https://github.com/jlowin/fastmcp)**: Excellent MCP server framework
- **[Model Context Protocol](https://modelcontextprotocol.io/)**: Standardized AI-application communication

---

**Built with ❤️ with Claude**