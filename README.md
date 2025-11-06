<div align="center">
  <img src="docs/img/ha-mcp-logo.png" alt="Home Assistant MCP Server Logo" width="300"/>

  # The Unofficial and Awesome Home Assistant MCP Server

  <p align="center">
    <a href="https://github.com/homeassistant-ai/ha-mcp"><img src="https://img.shields.io/badge/Home%20Assistant-Add--on-41BDF5?logo=home-assistant" alt="Home Assistant Add-on"></a>
    <a href="https://github.com/homeassistant-ai/ha-mcp/releases"><img src="https://img.shields.io/github/v/release/homeassistant-ai/ha-mcp" alt="Release"></a>
    <a href="https://github.com/homeassistant-ai/ha-mcp/actions/workflows/e2e-tests.yml"><img src="https://img.shields.io/github/actions/workflow/status/homeassistant-ai/ha-mcp/e2e-tests.yml?branch=master&label=E2E%20Tests" alt="E2E Tests"></a>
    <a href="LICENSE.md"><img src="https://img.shields.io/github/license/homeassistant-ai/ha-mcp.svg" alt="License"></a>
    <br>
    <a href="https://github.com/homeassistant-ai/ha-mcp/commits/master"><img src="https://img.shields.io/github/commit-activity/m/homeassistant-ai/ha-mcp.svg" alt="Activity"></a>
    <a href="https://github.com/jlowin/fastmcp"><img src="https://img.shields.io/badge/Built%20with-FastMCP-purple" alt="Built with FastMCP"></a>
    <img src="https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fhomeassistant-ai%2Fha-mcp%2Fmaster%2Fpyproject.toml" alt="Python Version">
  </p>

  <p align="center">
    <em>A comprehensive Model Context Protocol (MCP) server that enables AI assistants to interact with Home Assistant.<br>
    Using natural language, control smart home devices, query states, execute services and manage your automations.</em>
  </p>
</div>

---

![Home Assistant MCP Demo](docs/img/demo.webp)

**[YouTube version →](https://youtu.be/eCO93KfSvIM)**

---

## ✨ Features

### 🔍 Discover, Search and Query
- **Fuzzy Entity Search**: Comprehensive search with similar words tolerance
- **Deep Configuration Search**: Search within automation triggers, script sequences, and helper configurations
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
- **Backup and Restore**: Create fast local backups (excludes database) and restore with safety mechanisms

---

## 🚀 Installation

Choose the installation method that best fits your setup:

### Method 1: Home Assistant Add-on (Recommended)

**Best for:** Users running Home Assistant OS

**Advantages:**
- ✅ 5 clicks installation
- ✅ Isolated environment
- ✅ Automatic updates
- ✅ Part of your Home Assistant Setup

**Installation Steps:**

1. **Click the button to add the repository** to your Home Assistant instance:

   [![Add Repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fhomeassistant-ai%2Fha-mcp)

   Or manually add this repository URL in Supervisor → Add-on Store:
   ```
   https://github.com/homeassistant-ai/ha-mcp
   ```

2. **Navigate to the add-on** "Home Assistant MCP Server" from the add-on store

3. **Click Install, Wait and then Start**

4. Follow the [configuration instructions for clients in the add-on documentation](homeassistant-addon/DOCS.md)

---

### Method 2: Container

**Best for:** Recommended for Home Assistant Container or when Docker is available

**Advantages:**
- ✅ No installation
- ✅ Isolated environment
- ✅ Automatic updates

**Get a long-lived token:** Home Assistant → Your Profile → Security → Long-Lived Access Tokens

**Client Configuration:**

<details>
<summary><b>📱 Claude Desktop or any mcp.json format</b></summary>

**Location:**
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Add to your `mcp.json`:
```json
{
  "mcpServers": {
    "home-assistant": {
      "command": "docker",
      "args": [
        "run",
        "--rm",
        "-e", "HOMEASSISTANT_URL=http://homeassistant.local:8123",
        "-e", "HOMEASSISTANT_TOKEN=your_long_lived_token",
        "ghcr.io/homeassistant-ai/ha-mcp:latest"
      ]
    }
  }
}
```

</details>

<details>
<summary><b>🌐 Web Clients (Claude.ai, ChatGPT, etc.)</b></summary>

1. **Create a docker-compose.yml:**
   ```yaml
   version: '3.8'
   services:
     ha-mcp:
       image: ghcr.io/homeassistant-ai/ha-mcp:latest
       container_name: ha-mcp
       ports:
         - "8086:8086"
       environment:
         HOMEASSISTANT_URL: http://homeassistant.local:8123
         HOMEASSISTANT_TOKEN: your_long_lived_token
         MCP_SECRET_PATH: /__your_secret_string__
       command: ["fastmcp", "run", "fastmcp-webclient.json"]
       restart: unless-stopped

     cloudflared:
       image: cloudflare/cloudflared:latest
       command: tunnel --url http://ha-mcp:8086
       depends_on:
         - ha-mcp
   ```

2. **Start the services:**
   ```bash
   docker compose up -d
   ```

3. **Check cloudflared logs for your URL:**
   ```bash
   docker compose logs cloudflared
   ```

4. **Use:** `https://abc-def.trycloudflare.com/__your_secret_string__`

</details>

<details>
<summary><b>💻 Claude Code</b></summary>

```bash
claude mcp add-json home-assistant '{
  "command": "docker",
  "args": [
    "run",
    "--rm",
    "-e", "HOMEASSISTANT_URL=http://homeassistant.local:8123",
    "-e", "HOMEASSISTANT_TOKEN=your_long_lived_token",
    "ghcr.io/homeassistant-ai/ha-mcp:latest"
  ]
}'
```

</details>

---

### Method 3: Running Python with UV

**Best for:** When Docker is not available

> **Windows users:** Follow the [Windows UV setup guide](docs/Windows-uv-guide.md)

**Prerequisites:**
- [UV package manager](https://docs.astral.sh/uv/getting-started/installation/)
  - Windows: winget install astral-sh.uv -e
  - MacOS: brew install uv
- Your Home assistant URL (ex: http://localhost:8123) for HOMEASSISTANT_URL variable
- A Home Assistant long-lived access token (Profile → Security → Long-Lived Access Tokens) for HOMEASSISTANT_TOKEN variable

**Client Configuration:**

<details>
<summary><b>📱 Claude Desktop or any mcp.json format</b></summary>

**Config file:**
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "Home Assistant": {
      "command": "uvx",
      "args": ["ha-mcp"],
      "env": {
        "HOMEASSISTANT_URL": "http://localhost:8123",
        "HOMEASSISTANT_TOKEN": "your_long_lived_token"
      }
    }
  }
}
```
Note: replace both HOMEASSISTANT_URL and HOMEASSISTANT_TOKEN with your values.

</details>

<details>
<summary><b>💻 Claude Code</b></summary>

```bash
claude mcp add --transport stdio home-assistant \
  --env HOMEASSISTANT_URL=http://localhost:8123 \
  --env HOMEASSISTANT_TOKEN=your_long_lived_token \
  -- uvx ha-mcp
```

</details>

<details>
<summary><b>🌐 Web Clients (Claude.ai, ChatGPT, etc.)</b></summary>

Run the MCP server with uvx (replace the values of the environement variables):

Windows:
```bash
set HOMEASSISTANT_URL=http://localhost:8123
set HOMEASSISTANT_TOKEN=your_long_lived_token
set MCP_PORT=8086
set MCP_SECRET_PATH=/__my_secret__
uvx --from ha-mcp ha-mcp-web
```
Others:
```bash
export HOMEASSISTANT_URL=http://localhost:8123
export HOMEASSISTANT_TOKEN=your_long_lived_token
export MCP_PORT=8086
export MCP_SECRET_PATH=/__my_secret__
uvx --from ha-mcp ha-mcp-web
```

Web client required https and a public URL. You need to use a proxy in front of `http://localhost:8086`.

Easiest option is to download [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/#latest-release)

**In another terminal, start Cloudflare Tunnel:**

```bash
cloudflared tunnel --url http://localhost:8086
```

Use the public url provided and add your secret path like so `https://XYZ.trycloudflare.com/__my_secret__`. This url must be used in your Web client MCP configuration and kept secret.
</details>

**Development:** See [CONTRIBUTING.md](CONTRIBUTING.md) for testing and contribution guidelines.

---

## 🛠️ Available Tools

### Search & Discovery Tools
| Tool | Description | Example |
|------|-------------|---------|
| `ha_search_entities` | Comprehensive entity search with fuzzy matching | `ha_search_entities("lumiere salon")` |
| `ha_deep_search` | Search within automation/script/helper configurations | `ha_deep_search("light.turn_on")` |
| `ha_get_overview` | AI-optimized system overview with entity counts | `ha_get_overview()` |

### Core Home Assistant API Tools
| Tool | Description | Example |
|------|-------------|---------|
| `ha_get_state` | Get entity state with attributes and context | `ha_get_state("light.living_room")` |
| `ha_call_service` | Execute any Home Assistant service (universal control) | `ha_call_service("light", "turn_on", {"entity_id": "light.all"})` |

### Device Control Tools
| Tool | Description | Example |
|------|-------------|---------|
| `ha_bulk_control` | Control multiple devices with WebSocket verification | `ha_bulk_control([{"entity_id": "light.all", "action": "turn_on"}])` |
| `ha_get_operation_status` | Check status of device operations | `ha_get_operation_status("operation_id")` |
| `ha_get_bulk_status` | Check status of multiple operations | `ha_get_bulk_status(["op1", "op2"])` |

### Configuration Management Tools
| Tool | Description | Example |
|------|-------------|---------|
| `ha_config_set_helper` | Create/update helper entities | `ha_config_set_helper("input_boolean", "test")` |
| `ha_config_remove_helper` | Delete helper entities | `ha_config_remove_helper("input_boolean", "test")` |
| `ha_config_set_script` | Create/update scripts | `ha_config_set_script("script_id", config)` |
| `ha_config_get_script` | Get script configuration | `ha_config_get_script("script_id")` |
| `ha_config_remove_script` | Delete scripts | `ha_config_remove_script("script_id")` |
| `ha_config_set_automation` | Create/update automations | `ha_config_set_automation(config)` |
| `ha_config_get_automation` | Get automation configuration | `ha_config_get_automation("automation.id")` |
| `ha_config_remove_automation` | Delete automations | `ha_config_remove_automation("automation.id")` |

### History & Insights Tools
| Tool | Description | Example |
|------|-------------|---------|
| `ha_get_logbook` | Access historical logbook entries | `ha_get_logbook(hours_back=24)` |

### Backup & Restore Tools
| Tool | Description | Example |
|------|-------------|---------|
| `ha_backup_create` | Create fast local backup | `ha_backup_create("backup_name")` |
| `ha_backup_restore` | Restore from backup | `ha_backup_restore("backup_id")` |

### Template & Documentation Tools
| Tool | Description | Example |
|------|-------------|---------|
| `ha_eval_template` | Evaluate Jinja2 templates | `ha_eval_template("{{ states('sensor.temp') }}")` |
| `ha_get_domain_docs` | Get Home Assistant domain documentation | `ha_get_domain_docs("light")` |

---

## ⚙️ Configuration Options

### Environment Variables

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `HOMEASSISTANT_URL` | Home Assistant URL | - | Yes |
| `HOMEASSISTANT_TOKEN` | Long-lived access token | - | Yes |
| `BACKUP_HINT` | Backup recommendation level | `normal` | No |

**Backup Hint Modes:**
- `strong`: Suggests backup before first modification each day/session
- `normal`: Suggests backup only before irreversible operations (recommended)
- `weak`: Rarely suggests backups
- `auto`: Same as normal (future: auto-detection)

---

## 🤝 Contributing

For development setup, testing instructions, and contribution guidelines, see **[CONTRIBUTING.md](CONTRIBUTING.md)**.

For comprehensive testing documentation, see **[tests/README.md](tests/README.md)**.

---

## 🛣️ Development Roadmap

### Completed ✅
- [x] Core infrastructure and HTTP client
- [x] FastMCP integration with OpenAPI auto-generation
- [x] Smart search tools with fuzzy matching
- [x] Optimized tool documentation to reduce tool call errors
- [x] WebSocket async device control
- [x] Logbook history and operational insights
- [x] Comprehensive test suite
- [x] Home Assistant Add-on support
- [x] Docker images with multi-mode support

For future enhancements and planned features, see the [Development Roadmap](https://github.com/homeassistant-ai/ha-mcp/wiki) in our wiki.

---

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## 🙏 Acknowledgments

- **[Home Assistant](https://home-assistant.io/)**: Amazing smart home platform (!)
- **[FastMCP](https://github.com/jlowin/fastmcp)**: Excellent MCP server framework
- **[Model Context Protocol](https://modelcontextprotocol.io/)**: Standardized AI-application communication
- **[Claude Code](https://github.com/anthropics/claude-code)**: AI-powered coding assistant

## 👥 Contributors

- **[@julienld](https://github.com/julienld)** — Project maintainer & core contributor.
- **[@kingbear2](https://github.com/kingbear2)** — Windows UV setup guide.
