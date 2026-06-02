> **Breaking change (v7.3.0):** `ha_config_set_yaml` has been moved to [beta](docs/beta.md).

<div align="center">
  <img src="docs/img/ha-mcp-logo.png" alt="Home Assistant MCP Server Logo" width="300"/>

  # The Unofficial and Awesome Home Assistant MCP Server

  <!-- mcp-name: io.github.homeassistant-ai/ha-mcp -->

  <p align="center">
    <img src="https://img.shields.io/badge/tools-84-blue" alt="95+ Tools">
    <a href="https://github.com/homeassistant-ai/ha-mcp/releases"><img src="https://img.shields.io/github/v/release/homeassistant-ai/ha-mcp" alt="Release"></a>
    <a href="https://github.com/homeassistant-ai/ha-mcp/actions/workflows/e2e-tests.yml"><img src="https://img.shields.io/github/actions/workflow/status/homeassistant-ai/ha-mcp/e2e-tests.yml?branch=master&label=E2E%20Tests" alt="E2E Tests"></a>
    <a href="LICENSE.md"><img src="https://img.shields.io/github/license/homeassistant-ai/ha-mcp.svg" alt="License"></a>
    <br>
    <a href="https://github.com/homeassistant-ai/ha-mcp/commits/master"><img src="https://img.shields.io/github/commit-activity/m/homeassistant-ai/ha-mcp.svg" alt="Activity"></a>
    <a href="https://github.com/jlowin/fastmcp"><img src="https://img.shields.io/badge/Built%20with-FastMCP-purple" alt="Built with FastMCP"></a>
    <img src="https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fhomeassistant-ai%2Fha-mcp%2Fmaster%2Fpyproject.toml" alt="Python Version">
    <a href="https://github.com/sponsors/julienld"><img src="https://img.shields.io/badge/GitHub_Sponsors-☕-blueviolet" alt="GitHub Sponsors"></a>
    <a href="https://homeassistant-ai.github.io/ha-mcp/"><img src="https://img.shields.io/badge/Website-docs-teal" alt="Website"></a>
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
<a href="https://homeassistant-ai.github.io/ha-mcp/guide-macos/"><img src="https://img.shields.io/badge/Setup_Guide_for_macOS-000000?style=for-the-badge&logo=apple&logoColor=white" alt="Setup Guide for macOS" height="120"></a>&nbsp;&nbsp;&nbsp;&nbsp;<a href="https://homeassistant-ai.github.io/ha-mcp/guide-linux/"><img src="https://img.shields.io/badge/Setup_Guide_for_Linux-FCC624?style=for-the-badge&logo=linux&logoColor=black" alt="Setup Guide for Linux" height="120"></a>&nbsp;&nbsp;&nbsp;&nbsp;<a href="https://homeassistant-ai.github.io/ha-mcp/guide-windows/"><img src="https://img.shields.io/badge/Setup_Guide_for_Windows-0078D6?style=for-the-badge&logo=windows&logoColor=white" alt="Setup Guide for Windows" height="120"></a>
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
<summary><b>🐧 Linux</b></summary>

Anthropic doesn't ship Claude Desktop for Linux, so pick one path:

**Claude Desktop** — free, via the community build:

1. Install the community [Claude Desktop for Linux](https://github.com/aaddrick/claude-desktop-debian) build and sign in with a free [claude.ai](https://claude.ai) account
2. Open **Terminal** and run:
   ```sh
   curl -LsSf https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/master/scripts/install-linux.sh | sh
   ```
3. Restart Claude Desktop, then ask: **"Can you see my Home Assistant?"**

**Claude Code** — official CLI, requires a paid Claude plan:

1. Install Claude Code: `curl -fsSL https://claude.ai/install.sh | bash`
2. Configure ha-mcp, then run `claude`:
   ```sh
   curl -LsSf https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/master/scripts/install.sh | sh -s -- --claude-code
   ```
3. Start `claude`, run `/mcp` to confirm, then ask: **"Can you see my Home Assistant?"**

[Full Linux guide →](https://homeassistant-ai.github.io/ha-mcp/guide-linux/)

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

<details>
<summary><b>🏠 Home Assistant OS (Add-on)</b></summary>

1. Add the repository to your Home Assistant instance:

   [![Add Repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fhomeassistant-ai%2Fha-mcp)

2. Install **"Home Assistant MCP Server"** from the Add-on Store and wait for it to complete
3. Click **Start**, then open the **Logs** tab to find your unique MCP URL
4. Configure your AI client with that URL

No token or credential setup needed — the add-on connects to Home Assistant automatically.

[Full add-on documentation →](homeassistant-addon/DOCS.md)

</details>

<details>
<summary><b>🌐 Remote Access (Nabu Casa / Webhook Proxy)</b></summary>

Already have **Nabu Casa** or another reverse proxy pointing at your Home Assistant? The Webhook Proxy add-on routes MCP traffic through your existing setup — no separate tunnel or port forwarding needed.

1. Install the **MCP Server add-on** (see above) and the **Webhook Proxy** add-on from the same store
2. Start the webhook proxy and **restart Home Assistant** when prompted
3. Copy the webhook URL from the add-on logs:
   ```
   MCP Server URL (remote): https://xxxxx.ui.nabu.casa/api/webhook/mcp_xxxxxxxx
   ```
4. Configure your AI client with that URL

For other remote access methods (Cloudflare Tunnel, custom reverse proxy), see the [Setup Wizard](https://homeassistant-ai.github.io/ha-mcp/setup/).

[Webhook proxy documentation →](https://github.com/homeassistant-ai/ha-mcp/blob/master/homeassistant-addon-webhook-proxy/DOCS.md)

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

<details>
<!-- TOOLS_TABLE_START -->

<summary><b>Complete Tool List (84 tools)</b></summary>

| Category | Tools |
|----------|-------|
| **Add-ons** | `ha_get_addon`, `ha_manage_addon` |
| **Areas & Floors** | `ha_list_floors_areas`, `ha_remove_area_or_floor`, `ha_set_area_or_floor` |
| **Assist** | `ha_manage_pipeline` |
| **Automations** | `ha_config_get_automation`, `ha_config_remove_automation`, `ha_config_set_automation` |
| **Blueprints** | `ha_get_blueprint`, `ha_import_blueprint` |
| **Calendar** | `ha_config_get_calendar_events`, `ha_config_remove_calendar_event`, `ha_config_set_calendar_event` |
| **Camera** | `ha_get_camera_image` |
| **Dashboard** | `ha_get_dashboard_screenshot` *(beta)* |
| **Dashboards** | `ha_config_delete_dashboard_resource`, `ha_config_delete_dashboard`, `ha_config_get_dashboard`, `ha_config_list_dashboard_resources`, `ha_config_set_dashboard_resource`, `ha_config_set_dashboard` |
| **Device Registry** | `ha_get_device`, `ha_remove_device`, `ha_set_device` |
| **Energy** | `ha_manage_energy_prefs` |
| **Entity Registry** | `ha_get_entity_exposure`, `ha_get_entity`, `ha_remove_entity`, `ha_set_entity` |
| **Files** | `ha_delete_file` *(beta)*, `ha_list_files` *(beta)*, `ha_read_file` *(beta)*, `ha_write_file` *(beta)* |
| **Groups** | `ha_config_list_groups`, `ha_config_remove_group`, `ha_config_set_group` |
| **HACS** | `ha_get_hacs_info`, `ha_manage_hacs` |
| **Helper Entities** | `ha_config_list_helpers`, `ha_config_set_helper`, `ha_remove_helpers_integrations` |
| **History & Statistics** | `ha_get_automation_traces`, `ha_get_history`, `ha_get_logs` |
| **Integrations** | `ha_get_integration`, `ha_get_system_health`, `ha_set_integration_enabled` |
| **Labels & Categories** | `ha_config_get_category`, `ha_config_get_label`, `ha_config_remove_category`, `ha_config_remove_label`, `ha_config_set_category`, `ha_config_set_label` |
| **Scenes** | `ha_config_get_scene`, `ha_config_remove_scene`, `ha_config_set_scene` |
| **Scripts** | `ha_config_get_script`, `ha_config_remove_script`, `ha_config_set_script` |
| **Search & Discovery** | `ha_deep_search`, `ha_get_overview`, `ha_get_state`, `ha_search_entities` |
| **Service & Device Control** | `ha_bulk_control`, `ha_call_event`, `ha_call_service`, `ha_get_operation_status`, `ha_list_services` |
| **System** | `ha_config_set_yaml` *(beta)*, `ha_get_updates`, `ha_manage_backup`, `ha_manage_custom_tool` *(beta)*, `ha_reload_core`, `ha_restart` |
| **Todo Lists** | `ha_get_todo`, `ha_remove_todo_item`, `ha_set_todo_item` |
| **Utilities** | `ha_eval_template`, `ha_install_mcp_tools` *(beta)*, `ha_report_issue` |
| **Zones** | `ha_get_zone`, `ha_remove_zone`, `ha_set_zone` |

<!-- TOOLS_TABLE_END -->
</details>

---

## 🔌 Custom Component (ha_mcp_tools) *(beta)*

Some tools require a companion custom component installed in Home Assistant. Standard HA APIs do not expose file system access or YAML config editing. This component provides both.

**Tools that require the component:**

| Tool | Description |
|------|-------------|
| `ha_config_set_yaml` *(beta)* | Safely add, replace, or remove top-level YAML keys in `configuration.yaml` and package files (automatic backup, validation, and config check) |
| `ha_list_files` *(beta)* | List files in allowed directories (www/, themes/, custom_templates/) |
| `ha_read_file` *(beta)* | Read files from allowed paths (config YAML, logs, www/, themes/, custom_templates/, custom_components/) |
| `ha_write_file` *(beta)* | Write files to allowed directories |
| `ha_delete_file` *(beta)* | Delete files from allowed directories |

All other tools work without the component. These five return an error with installation instructions if the component is missing.

These tools also require feature flags: `HAMCP_ENABLE_FILESYSTEM_TOOLS=true` (file tools) and `ENABLE_YAML_CONFIG_EDITING=true` (YAML editing). To enable the `ha_install_mcp_tools` installer tool, set `HAMCP_ENABLE_CUSTOM_COMPONENT_INTEGRATION=true`.

### Install using HACS (recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=homeassistant-ai&repository=ha-mcp&category=integration)

To add manually: open **HACS** > **Integrations** > three-dot menu > **Custom repositories** > add `https://github.com/homeassistant-ai/ha-mcp` (category: Integration) > **Download**.

After installing, restart Home Assistant. Then open **Settings** > **Devices & Services** > **Add Integration** and search for **HA MCP Tools**.

### Install manually

Copy `custom_components/ha_mcp_tools/` from this repository into your HA `config/custom_components/` directory. Restart Home Assistant, then add the integration as described above.

---

## 🧠 Better Results with Agent Skills

This server gives your AI agent tools to control Home Assistant. For better configurations, pair it with [Home Assistant Agent Skills](https://github.com/homeassistant-ai/skills) — domain knowledge that teaches the agent Home Assistant best practices.

An MCP server can create automations, helpers, and dashboards, but it has no opinion on *how* to structure them. Without domain knowledge, agents tend to over-rely on templates, pick the wrong helper type, or produce automations that are hard to maintain. The skills fill that gap: native constructs over Jinja2 workarounds, correct helper selection, safe refactoring workflows, and proper use of automation modes.

### Bundled Skills (built-in)

Skills from `homeassistant-ai/skills` are bundled and served as [MCP resources](https://modelcontextprotocol.io/docs/concepts/resources) via `skill://` URIs. Any MCP client that supports resources can discover them automatically — no manual installation needed. For tool-only clients (claude.ai, etc.), the same skills are reachable through the polymorphic `ha_get_skill_guide` tool — call it with no args to list bundled skills, with a `skill` arg to list its files, or with `skill` + `file` to read content. Resources are not auto-injected into context — clients must explicitly request them, so idle context cost is just the metadata listing.

`ha_get_skill_guide` is a mandatory tool: the catalog always exposes it (it can't be disabled) so tool-only clients never see a silently missing skill surface.

Skills can still be installed manually for clients that prefer local skill files — see the [skills repo](https://github.com/homeassistant-ai/skills) for instructions.

---

## 🔍 Tool Discovery for AI Agents

By default, the full tool catalog (~86 tools) is listed to the client through the standard MCP `tools/list` response. Clients with deferred / on-demand tool loading (Claude Sonnet, Claude Opus,) handle that fine — tools are pulled into context only when needed, so idle context cost is near-zero.

For models *without* deferred tool support — Claude Haiku, Gemini, ChatGPT OpenAI-compatible local models, smaller open-weights models — listing 86 tools eats ~46K tokens of idle context. To address that, the server ships with a **search-based discovery mode** built on top of FastMCP's BM25 search transform.

### Enable search-based discovery

Set ENABLE_TOOL_SEARCH=true (or toggle the option in the HA add-on). The full catalog is replaced in the tool list with four entry points plus a small set of always-visible "pinned" tools (ha_search_entities, ha_get_overview, ha_restart, etc.). All tools remain callable directly by name once discovered:

| Tool | Purpose |
|------|---------|
| `ha_search_tools` | BM25 keyword search across all tools. Returns name, description, parameters, and annotations (`readOnlyHint` / `destructiveHint`) so the agent can pick the right one. |
| `ha_call_read_tool` | Execute a `readOnlyHint` tool by name. Safe — clients can auto-approve. |
| `ha_call_write_tool` | Execute a write tool that creates or updates data. |
| `ha_call_delete_tool` | Execute a tool that removes / deletes data. |

The proxy split lets MCP clients apply different permission policies per category (e.g. auto-approve reads, prompt for writes, confirm deletes) without parsing tool docstrings.

| Setting | Default | Description |
|---------|---------|-------------|
| `ENABLE_TOOL_SEARCH` | `false` | Replace full tool catalog with search-based discovery (~46K → ~5K idle tokens). |
| `TOOL_SEARCH_MAX_RESULTS` | `5` | Max results returned by `ha_search_tools` (range 2–10). |
| `PINNED_TOOLS` | empty | Comma-separated tool names to keep always visible. The web settings UI is the primary way to manage this. |

### When to enable

- **Claude Haiku, OpenAI-compatible local models, Gemini, ChatGPT or any model without native deferred tool support** — large idle-context savings.
- MCP clients that cap total tool count (some cap at 100) — surfaces a minimal set (~10 tools) instead of 86.
- **Cost-sensitive deployments** — fewer idle tokens per turn.

Leave it off when using Claude Sonnet/Opus or any client with deferred tool loading; the full catalog has no idle cost there and direct calls skip the search step. If you choose to use our toolsearch then you should disable the native Claude Opus/Sonnet toolsearch, which is called deferred tools in the settings.

For the HA add-on, the same option is documented in [`homeassistant-addon/DOCS.md`](homeassistant-addon/DOCS.md#enable_tool_search) along with the in-add-on settings UI for fine-grained tool enable/disable/pin.

---

## 🧪 Dev Channel

Want early access to new features and fixes? Dev releases (`.devN`) are published on every push to master.

**[Dev Channel Documentation](docs/dev-channel.md)** — Instructions for pip/uvx, Docker, and Home Assistant add-on.

---

## 🤝 Contributing

For development setup, testing instructions, and contribution guidelines, see **[CONTRIBUTING.md](CONTRIBUTING.md)**.

For comprehensive testing documentation, see **[tests/README.md](tests/README.md)**.

---

## 🔒 Privacy

Ha-mcp runs **locally** on your machine. Your smart home data stays on your network.

- **No telemetry today** — anonymous usage stats are a planned future feature (as of May 2026); users will be notified when it lands and it will be opt-in only
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
- **[PolicyLayer](https://policylayer.com/)**: Argument-path predicate DSL shape (`args.domain in [...]` with `eq`/`in`/`regex`/`contains`/`exists`/...) inspired the per-tool approval rule schema (#966).

## 👥 Contributors

### Maintainers

- **[@julienld](https://github.com/julienld)** — Project creator.
- **[@sergeykad](https://github.com/sergeykad)** — Core maintainer.
- **[@kingpanther13](https://github.com/kingpanther13)** — Core maintainer.
- **[@Patch76](https://github.com/Patch76)** — Core maintainer.

### Contributors

- **[@bigeric08](https://github.com/bigeric08)** — Explicit `mcp` dependency for protocol version 2025-11-25 support.
- **[@airlabno](https://github.com/airlabno)** — Support for `data` field in schedule time blocks.
- **[@ryphez](https://github.com/ryphez)** — Codex Desktop UI MCP quick setup guide.
- **[@Danm72](https://github.com/Danm72)** — Entity registry tools (`ha_set_entity`, `ha_get_entity`) for managing entity properties.
- **[@Raygooo](https://github.com/Raygooo)** — SOCKS proxy support.
- **[@cj-elevate](https://github.com/cj-elevate)** — Integration & entity management tools (enable/disable/delete); person/zone/tag config store routing.
- **[@maxperron](https://github.com/maxperron)** — Beta testing.
- **[@kingbear2](https://github.com/kingbear2)** — Windows UV setup guide.
- **[@konradwalsh](https://github.com/konradwalsh)** — Financial support via [GitHub Sponsors](https://github.com/sponsors/julienld). Thank you! ☕
- **[@knowald](https://github.com/knowald)** — Area resolution via device registry in `ha_get_system_overview` for entities assigned through their parent device. Financial support via [GitHub Sponsors](https://github.com/sponsors/julienld). Thank you! ☕
- **[@zorrobyte](https://github.com/zorrobyte)** — Per-client WebSocket credentials in OAuth mode, fixing WebSocket tool failures.
- **[@deanbenson](https://github.com/deanbenson)** — Fixed `ha_deep_search` timeout on large Home Assistant instances with many automations.
- **[@saphid](https://github.com/saphid)** — Config entry options flow tools (initial design, #590).
- **[@adraguidev](https://github.com/adraguidev)** — Fix menu-based config entry flows for group helpers (#647).
- **[@transportrefer](https://github.com/transportrefer)** — Integration options inspection (`ha_get_integration` schema support, #689).
- **[@teh-hippo](https://github.com/teh-hippo)** — Fix blueprint import missing save step.
- **[@smenzer](https://github.com/smenzer)** — Documentation fix.
- **[@The-Greg-O](https://github.com/The-Greg-O)** — REST API for config entry deletion.
- **[@restriction](https://github.com/restriction)** — Responsible disclosure: python_transform sandbox missing call target validation.
- **[@lcrostarosa](https://github.com/lcrostarosa)** — Diagnostic and health monitoring tools concept (#675), inspiring system/error logs, repairs, and ZHA radio metrics integration.
- **[@roysha1](https://github.com/roysha1)** — Copilot CLI support in the installation wizard; replaced placeholder logo SVGs with real brand icons on the documentation site.
- **[@teancom](https://github.com/teancom)** — Fix add-on stats endpoint (`/addons/{slug}/stats`).
- **[@TomasDJo](https://github.com/TomasDJo)** — Category support for automations, scripts, and scenes.
- **[@bzelch](https://github.com/bzelch)** — `python_transform` support for automations and scripts.
- **[@gcormier](https://github.com/gcormier)** — Windows installer improvements: removed unused variable and fixed terminal closing after install.
- **[@ekobres](https://github.com/ekobres)** — Feature flags for `HAMCP_ENABLE_FILESYSTEM_TOOLS` and `HAMCP_ENABLE_CUSTOM_COMPONENT_INTEGRATION` in the add-on config, with beta tagging in source and docs.
- **[@w3z315](https://github.com/w3z315)** — Financial support via [GitHub Sponsors](https://github.com/sponsors/julienld). Thank you! ☕
- **[@griffinmartin](https://github.com/griffinmartin)** — Added OpenCode (by Anomaly) as a selectable AI client in the setup wizard, with both stdio and streamable HTTP support.
- **[@hhopke](https://github.com/hhopke)** — Fixed addon API calls to route through HA Core ingress proxy instead of direct container connections, fixing `ha_manage_addon` proxy mode on addon installs.
- **[@tomwilkie](https://github.com/tomwilkie)** — JMESPath middleware exploration (#1147) whose review-time token-measurement data informed the design of #1199 and #1225.
- **[@SealKan](https://github.com/SealKan)** — `fields=`/`attribute_keys=` projection on six read-heavy tools (#1225), `ha_call_event` tool (#1239), and dashboards-list helper refactor (#1207).
- **[@KarelTestSpecial](https://github.com/KarelTestSpecial)** — Cached YAML instance to prevent CPU spikes during bulk edits (#1371).
- **[@corgan2222](https://github.com/corgan2222)** — HA brand assets for custom integration (#1317).
- **[@drseanwing](https://github.com/drseanwing)** — Progress emission via FastMCP `Context` in long-running tools (#1124); tool-discovery / categorized-search docs (#1123).
- **[@fnordpig](https://github.com/fnordpig)** — Config subentry support (#1393) and Assist pipeline management tool (#1392).
- **[@paul43210](https://github.com/paul43210)** — `array_patch` mode in `ha_manage_addon` for atomic GET-modify-POST (#1063).
- **[@L1AD](https://github.com/L1AD)** — Filed #966 proposing tool security policies; pointed to PolicyLayer's MCP-security work as prior art that inspired the predicate DSL shape.

---

## 💬 Community

- **[GitHub Discussions](https://github.com/homeassistant-ai/ha-mcp/discussions)** — Ask questions, share ideas
- **[Issue Tracker](https://github.com/homeassistant-ai/ha-mcp/issues)** — Report bugs, request features, or suggest tool behavior improvements

---

## ⭐ Star History

[![Star History Chart](https://api.star-history.com/svg?repos=homeassistant-ai/ha-mcp&type=Date)](https://star-history.com/#homeassistant-ai/ha-mcp&Date)
