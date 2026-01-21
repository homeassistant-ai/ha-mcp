<div align="center">
  <img src="docs/img/ha-mcp-logo.png" alt="Home Assistant MCP Server Logo" width="300"/>

  # The Unofficial and Awesome Home Assistant MCP Server

  <!-- mcp-name: io.github.homeassistant-ai/ha-mcp -->

  <p align="center">
    <img src="https://img.shields.io/badge/tools-80+-blue" alt="80+ Tools">
    <a href="https://github.com/homeassistant-ai/ha-mcp/releases"><img src="https://img.shields.io/github/v/release/homeassistant-ai/ha-mcp" alt="Release"></a>
    <a href="https://github.com/homeassistant-ai/ha-mcp/actions/workflows/e2e-tests.yml"><img src="https://img.shields.io/github/actions/workflow/status/homeassistant-ai/ha-mcp/e2e-tests.yml?branch=master&label=E2E%20Tests" alt="E2E Tests"></a>
    <a href="LICENSE.md"><img src="https://img.shields.io/github/license/homeassistant-ai/ha-mcp.svg" alt="License"></a>
    <br>
    <a href="https://github.com/homeassistant-ai/ha-mcp/commits/master"><img src="https://img.shields.io/github/commit-activity/m/homeassistant-ai/ha-mcp.svg" alt="Activity"></a>
    <a href="https://github.com/jlowin/fastmcp"><img src="https://img.shields.io/badge/Built%20with-FastMCP-purple" alt="Built with FastMCP"></a>
    <img src="https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fhomeassistant-ai%2Fha-mcp%2Fmaster%2Fpyproject.toml" alt="Python Version">
    <a href="https://github.com/sponsors/julienld"><img src="https://img.shields.io/badge/GitHub_Sponsors-☕-blueviolet" alt="GitHub Sponsors"></a>
  </p>

  <p align="center">
    <em>A comprehensive Model Context Protocol (MCP) server that enables AI assistants to interact with Home Assistant.<br>
    Using natural language, control smart home devices, query states, execute services and manage your automations.</em>
  </p>
</div>

---

![Demo with Claude Desktop](docs/img/demo.webp)

---

## 🚀 Get Started

### Full guide to get you started with Claude Desktop (~10 min)

*No paid subscription required.* Click on your operating system:

<p>
<a href="https://homeassistant-ai.github.io/ha-mcp/guide-macos/"><img src="https://img.shields.io/badge/Setup_Guide_for_macOS-000000?style=for-the-badge&logo=apple&logoColor=white" alt="Setup Guide for macOS" height="120"></a>&nbsp;&nbsp;&nbsp;&nbsp;<a href="https://homeassistant-ai.github.io/ha-mcp/guide-windows/"><img src="https://img.shields.io/badge/Setup_Guide_for_Windows-0078D6?style=for-the-badge&logo=windows&logoColor=white" alt="Setup Guide for Windows" height="120"></a>
</p>

### Quick install (~5 min)

<details>
<summary><b>🍎 macOS</b></summary>

1. Go to [claude.ai](https://claude.ai) and sign in (or create a free account)
2. Open **Terminal** and run:
   ```sh
   curl -LsSf https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/master/scripts/install-macos.sh | sh
   ```
3. [Download Claude Desktop](https://claude.ai/download) (or restart: Claude menu → Quit)
4. Ask Claude: **"Can you see my Home Assistant?"**

You're now connected to the demo environment! [Connect your own Home Assistant →](https://homeassistant-ai.github.io/ha-mcp/guide-macos/#step-6-connect-your-home-assistant)

</details>

<details>
<summary><b>🪟 Windows</b></summary>

1. Go to [claude.ai](https://claude.ai) and sign in (or create a free account)
2. Open **Windows PowerShell** (from Start menu) and run:
   ```powershell
   irm https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/master/scripts/install-windows.ps1 | iex
   ```
3. [Download Claude Desktop](https://claude.ai/download) (or restart: File → Exit)
4. Ask Claude: **"Can you see my Home Assistant?"**

You're now connected to the demo environment! [Connect your own Home Assistant →](https://homeassistant-ai.github.io/ha-mcp/guide-windows/#step-6-connect-your-home-assistant)

</details>

### 🧙 Setup Wizard for 15+ clients

**Claude Code, Gemini CLI, ChatGPT, Open WebUI, VSCode, Cursor, and more.**

<p>
<a href="https://homeassistant-ai.github.io/ha-mcp/setup/"><img src="https://img.shields.io/badge/Open_Setup_Wizard-4A90D9?style=for-the-badge" alt="Open Setup Wizard" height="40"></a>
</p>

Having issues? Check the **[FAQ & Troubleshooting](https://homeassistant-ai.github.io/ha-mcp/faq/)**

---

## 💬 What Can You Do With It?

Just talk to Claude naturally. Here are some real examples:

| You Say | What Happens |
|---------|--------------|
| *"Create an automation that turns on the porch light at sunset"* | Creates the automation with proper triggers and actions |
| *"Add a weather card to my dashboard"* | Updates your Lovelace dashboard with the new card |
| *"The motion sensor automation isn't working, debug it"* | Analyzes execution traces, identifies the issue, suggests fixes |
| *"Make my morning routine automation also turn on the coffee maker"* | Reads the existing automation, adds the new action, updates it |
| *"Create a script that sets movie mode: dim lights, close blinds, turn on TV"* | Creates a reusable script with the sequence of actions |

Spend less time configuring, more time enjoying your smart home.

---

## ✨ Features

| Category | Capabilities |
|----------|--------------|
| **🔍 Search** | Fuzzy entity search, deep config search, system overview |
| **🏠 Control** | Any service, bulk device control, real-time states |
| **🔧 Manage** | Automations, scripts, helpers, dashboards, areas, zones, groups, calendars, blueprints |
| **📊 Monitor** | History, statistics, camera snapshots, automation traces, ZHA devices |
| **💾 System** | Backup/restore, updates, add-ons, device registry |
| **🔐 Auth** | OAuth 2.1 with DCR for secure multi-user access (Claude.ai), token-based for local clients |

<details>
<summary><b>🛠️ Complete Tool List (82 tools)</b></summary>

| Category | Tools |
|----------|-------|
| **Search & Discovery** | `ha_search_entities`, `ha_deep_search`, `ha_get_overview`, `ha_get_state` |
| **Service & Device Control** | `ha_call_service`, `ha_bulk_control`, `ha_get_operation_status`, `ha_get_bulk_status`, `ha_list_services` |
| **Automations** | `ha_config_get_automation`, `ha_config_set_automation`, `ha_config_remove_automation` |
| **Scripts** | `ha_config_get_script`, `ha_config_set_script`, `ha_config_remove_script` |
| **Helper Entities** | `ha_config_list_helpers`, `ha_config_set_helper`, `ha_config_remove_helper` |
| **Dashboards** | `ha_config_get_dashboard`, `ha_config_set_dashboard`, `ha_config_update_dashboard_metadata`, `ha_config_delete_dashboard`, `ha_get_dashboard_guide`, `ha_get_card_types`, `ha_get_card_documentation` |
| **Areas & Floors** | `ha_config_list_areas`, `ha_config_set_area`, `ha_config_remove_area`, `ha_config_list_floors`, `ha_config_set_floor`, `ha_config_remove_floor` |
| **Labels** | `ha_config_get_label`, `ha_config_set_label`, `ha_config_remove_label`, `ha_manage_entity_labels` |
| **Zones** | `ha_get_zone`, `ha_create_zone`, `ha_update_zone`, `ha_delete_zone` |
| **Groups** | `ha_config_list_groups`, `ha_config_set_group`, `ha_config_remove_group` |
| **Todo Lists** | `ha_get_todo`, `ha_add_todo_item`, `ha_update_todo_item`, `ha_remove_todo_item` |
| **Calendar** | `ha_config_get_calendar_events`, `ha_config_set_calendar_event`, `ha_config_remove_calendar_event` |
| **Blueprints** | `ha_list_blueprints`, `ha_get_blueprint`, `ha_import_blueprint` |
| **Device Registry** | `ha_get_device`, `ha_update_device`, `ha_remove_device`, `ha_rename_entity` |
| **ZHA & Integrations** | `ha_get_zha_devices`, `ha_get_entity_integration_source` |
| **Add-ons** | `ha_get_addon` |
| **Camera** | `ha_get_camera_image` |
| **History & Statistics** | `ha_get_history`, `ha_get_statistics` |
| **Automation Traces** | `ha_get_automation_traces` |
| **System & Updates** | `ha_check_config`, `ha_restart`, `ha_reload_core`, `ha_get_system_info`, `ha_get_system_health`, `ha_get_updates` |
| **Backup & Restore** | `ha_backup_create`, `ha_backup_restore` |
| **Utility** | `ha_get_logbook`, `ha_eval_template`, `ha_get_domain_docs`, `ha_get_integration` |

</details>

---

## 🧪 Dev Channel

Want to test the latest changes before they hit stable? Dev releases (`.devN`) are published on every push to master.

```bash
# Install dev version
pip install ha-mcp --pre
# or with uv
uv pip install ha-mcp --pre

# Switch back to stable
pip install ha-mcp
```

Dev releases contain the newest features and fixes not yet in the weekly stable release (Tuesdays 10:00 UTC).

---

## 🤝 Contributing

For development setup, testing instructions, and contribution guidelines, see **[CONTRIBUTING.md](CONTRIBUTING.md)**.

For comprehensive testing documentation, see **[tests/README.md](tests/README.md)**.

---

## 🔒 Privacy

Ha-mcp runs **locally** on your machine. Your smart home data stays on your network.

- **Configurable telemetry** — optional anonymous usage stats
- **No personal data collection** — we never collect entity names, configs, or device data
- **User-controlled bug reports** — only sent with your explicit approval

For full details, see our [Privacy Policy](PRIVACY.md).

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
- **[@sergeykad](https://github.com/sergeykad)** — Dashboard card-level CRUD operations, better changelogs and removed the dependency to textdistance/numpy.
- **[@konradwalsh](https://github.com/konradwalsh)** — Financial support via [GitHub Sponsors](https://github.com/sponsors/julienld). Thank you! ☕
- **[@cj-elevate](https://github.com/cj-elevate)** — Integration & entity management tools (enable/disable/delete).
- **[@kingpanther13](https://github.com/kingpanther13)** — Native solutions guidance to reduce template overuse.
- **[@Raygooo](https://github.com/Raygooo)** — SOCKS proxy support.

---

## 💬 Community

- **[GitHub Discussions](https://github.com/homeassistant-ai/ha-mcp/discussions)** — Ask questions, share ideas
- **[Issue Tracker](https://github.com/homeassistant-ai/ha-mcp/issues)** — Report bugs, request features, or suggest tool behavior improvements

---

## ⭐ Star History

[![Star History Chart](https://api.star-history.com/svg?repos=homeassistant-ai/ha-mcp&type=Date)](https://star-history.com/#homeassistant-ai/ha-mcp&Date)
