> **Breaking change (v7.3.0):** `ha_config_set_yaml` has been moved to [beta](docs/beta.md).

<div align="center">
  <img src="docs/img/ha-mcp-logo.png" alt="Home Assistant MCP Server Logo" width="300"/>

  # The Unofficial and Awesome Home Assistant MCP Server

  <!-- mcp-name: io.github.homeassistant-ai/ha-mcp -->

  <p align="center">
    <img src="https://img.shields.io/badge/tools-85-blue" alt="95+ Tools">
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

The recommended way to run ha-mcp is the **HA-MCP Custom Component**. It installs into Home Assistant through HACS, runs the full server **in-process**, and works on **every** Home Assistant installation type — Home Assistant OS, Supervised, Container, and Core — with full feature parity. It is the easiest setup in every case, with no access token to manage.

**Add it to Home Assistant via HACS (the preferred install):**

[![Add HA-MCP to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=homeassistant-ai&repository=ha-mcp&category=integration)

**Quick start:**

1. Install the **HA-MCP Custom Component** from HACS — click the badge above, or in HACS open **Integrations → ⋮ → Custom repositories**, add `https://github.com/homeassistant-ai/ha-mcp` (category: **Integration**), then **Download**.
2. **Restart Home Assistant.**
3. Go to **Settings → Devices & Services → Add Integration**, search for **HA-MCP Custom Component**, choose **HA-MCP Server**, and click **Submit**. Creating the entry starts the server.
4. Copy the connect URL from the **HA-MCP Server** notification (it is also on the entry's **Configure** screen).
5. Paste that URL into your AI client — done.

**Connect URL.** The notification gives you a Home Assistant webhook URL for remote clients — `https://<your-ha-domain>/api/webhook/<webhook-id>` through Nabu Casa or any reverse proxy already pointed at Home Assistant (locally, `http://<ha-host>:8123/api/webhook/<webhook-id>`). For clients on the same network, the server is also reachable directly at `http://<ha-ip>:9584/private_<random>`.

- **Local only?** Turn off **Remote access via webhook** in the entry options — no webhook is registered at all, while the direct port and sidebar panel keep working.
- **Settings panel:** while the server runs, an admin-only **HA-MCP** panel appears in the Home Assistant sidebar for managing tools, feature flags, backups, and themes.
- **Optional authentication:** set **Webhook authentication** to `ha_auth` to require a Home Assistant account sign-in instead of using the secret URL as the credential.
- **Manual install (no HACS):** copy `custom_components/ha_mcp_tools/` from this repository into your Home Assistant `config/custom_components/` directory, then restart and add the integration as above.

The component's second entry type, **HA MCP Tools**, is optional but recommended — it provides the privileged file and YAML config services the server's file tools use.

[Full in-process server documentation →](docs/in-process-server.md) · [Setup Wizard for client-specific config →](https://homeassistant-ai.github.io/ha-mcp/setup/)

### 🏠 Home Assistant App (add-on)

Prefer to run ha-mcp as a Home Assistant **app / add-on**? On **Home Assistant OS** and **Supervised** installs it is a close second — no access token to manage, and it works with Claude Desktop, Claude.ai, ChatGPT, and any other MCP client on your local network or configured for remote access.

1. Add the repository to your Home Assistant instance:

   [![Add Repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fhomeassistant-ai%2Fha-mcp)

   If that opens the App Store without an add-repository dialog (a [known Home Assistant issue](https://github.com/home-assistant/my.home-assistant.io/issues/698)), add it manually: **App Store → ⋮ → Repositories**, then paste `https://github.com/homeassistant-ai/ha-mcp`.

2. Install **"Home Assistant MCP Server"** from the App Store and click **Start**. *(Home Assistant 2026.2 renamed "Add-ons" to "Apps"; on older versions this is the Add-on Store.)*
3. Open the **Logs** tab to find your unique MCP URL.
4. Connect your AI client to that URL — **no token or credential setup needed**.

[Full add-on documentation →](homeassistant-addon/DOCS.md)

> ⚠️ **Configure exactly one install method per client.** The custom component, the add-on, Docker/PyPI, and local stdio are independent ways to run the same server — pick one and point your AI client at that single URL. Keeping two entries for the same server in one client (for example a local `uvx ha-mcp@latest` entry with `HOMEASSISTANT_URL` / `HOMEASSISTANT_TOKEN` alongside an add-on or component URL) is a known cause of connection hangs.

### Other install methods

These run the server outside Home Assistant — useful for **Container** / **Core** installs (which can't run add-ons) or a separate host. The [Setup Wizard](https://homeassistant-ai.github.io/ha-mcp/setup/) generates the exact client-specific config for each.

- **Docker (HTTP server):** run `ghcr.io/homeassistant-ai/ha-mcp` in HTTP mode, pointed at your Home Assistant URL and a long-lived token, and connect your client to its secret URL. See the [Setup Wizard](https://homeassistant-ai.github.io/ha-mcp/setup/) for the full command and per-client config.
- **PyPI / uvx (HTTP server):** run the published `ha-mcp` package with `uvx ha-mcp@latest` (or pip) as a streamable-HTTP server the same way. Details in the [Setup Wizard](https://homeassistant-ai.github.io/ha-mcp/setup/).
- **Local stdio (advanced / legacy):** runs ha-mcp on your own machine over stdio. The one-command installers in the **Demo server** section below use this path; the [Setup Wizard](https://homeassistant-ai.github.io/ha-mcp/setup/) covers connecting it to your own Home Assistant.

  > ⚠️ **stdio has known transport issues.** The stdio transport has connection problems that streamable HTTP does not ([#1713](https://github.com/homeassistant-ai/ha-mcp/issues/1713)). It is recommended only for demo/testing tinkering — for a real setup, use the custom component or an HTTP method above.

<details>
<summary><b>🌐 Remote access (Nabu Casa / Webhook Proxy add-on)</b></summary>

> **Using the HA-MCP custom component?** You do not need this — the component has its own built-in webhook for remote access (see the **Get Started** quick start at the top). This proxy is for the **add-on / Docker / pip** server installs.

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

### 🧪 Demo server (Windows / macOS / Linux)

Want to try ha-mcp before connecting your own Home Assistant? *No paid subscription required.* These one-command scripts set up a local **stdio** connection to a hosted **demo** environment so you can see it working in a few minutes. Each script's **Connect your own Home Assistant** link then shows how to point it at your instance.

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
| **🔒 Safety** | Read Only Mode toggle, per-tool enable/disable, tool security policies (user approval), automatic edit backups |

<details>
<!-- TOOLS_TABLE_START -->

<summary><b>Complete Tool List (85 tools)</b></summary>

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
| **Matter** | `ha_manage_radio` |
| **Scenes** | `ha_config_get_scene`, `ha_config_remove_scene`, `ha_config_set_scene` |
| **Scripts** | `ha_config_get_script`, `ha_config_remove_script`, `ha_config_set_script` |
| **Search & Discovery** | `ha_get_overview`, `ha_get_state`, `ha_search` |
| **Service & Device Control** | `ha_bulk_control`, `ha_call_event`, `ha_call_service`, `ha_get_operation_status`, `ha_list_services` |
| **System** | `ha_config_set_yaml` *(beta)*, `ha_manage_backup`, `ha_manage_custom_tool` *(beta)*, `ha_manage_theme`, `ha_manage_updates`, `ha_reload_core`, `ha_restart` |
| **Todo Lists** | `ha_get_todo`, `ha_remove_todo_item`, `ha_set_todo_item` |
| **Utilities** | `ha_eval_template`, `ha_install_mcp_tools` *(beta)*, `ha_report_issue` |
| **Zones** | `ha_get_zone`, `ha_remove_zone`, `ha_set_zone` |

<!-- TOOLS_TABLE_END -->
</details>

---

## 🆚 ha-mcp vs. Home Assistant's built-in MCP Server

Home Assistant ships its own [MCP Server integration](https://www.home-assistant.io/integrations/mcp_server/). It is built on the **Assist** pipeline, so a connected MCP client can read and control the entities you have exposed to Assist and run the intents Assist understands — handy for voice-style control of already-exposed devices.

ha-mcp is a standalone server built for **configuring, building, and debugging** your smart home, not just controlling it. On top of device control, it adds capabilities the built-in integration does not have:

| Capability | Built-in MCP Server | ha-mcp |
|------------|:-------------------:|:------:|
| Control exposed devices, query states | Yes | Yes |
| Entity scope | Only entities exposed to Assist | Everything in Home Assistant |
| Create / edit automations, scripts, scenes | No | Yes |
| Build & edit dashboards | No | Yes |
| Debug automations from traces, read history & logs | No | Yes |
| Manage helpers, areas, zones, labels, groups | No | Yes |
| Backups, add-ons, HACS, device & entity registry | No | Yes |

**Rule of thumb:** Use the built-in integration for voice-style control of devices you have already exposed; use ha-mcp when you want an AI assistant that can also build and maintain your Home Assistant setup.

---

## 🔌 Custom Component (ha_mcp_tools) — File & YAML Services

The **HA-MCP Custom Component** also powers a set of privileged tools that standard Home Assistant APIs can't provide: file system access and YAML config editing. (The same component runs the full server in-process — that's the recommended install in the **Get Started** section at the top.) Its **HA MCP Tools** services entry enables the tools below.

**Tools that require the component:**

| Tool | Description |
|------|-------------|
| `ha_config_set_yaml` *(beta)* | Safely add, replace, or remove top-level YAML keys in `configuration.yaml` and package files (automatic backup, validation, and config check) |
| `ha_list_files` *(beta)* | List files in allowed directories |
| `ha_read_file` *(beta)* | Read files from allowed paths (config YAML, logs, and allowed directories) |
| `ha_write_file` *(beta)* | Write files to allowed directories |
| `ha_delete_file` *(beta)* | Delete files from allowed directories |

All other tools work without the component. These five return an error with installation instructions if the component is missing.

These tools also require feature flags: `HAMCP_ENABLE_FILESYSTEM_TOOLS=true` (file tools) and `ENABLE_YAML_CONFIG_EDITING=true` (YAML editing). To enable the `ha_install_mcp_tools` installer tool, set `HAMCP_ENABLE_CUSTOM_COMPONENT_INTEGRATION=true`.

### Install

Install the **HA MCP Tools** services entry from the same **HA-MCP Custom Component**:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=homeassistant-ai&repository=ha-mcp&category=integration)

To add manually: open **HACS** > **Integrations** > three-dot menu > **Custom repositories** > add `https://github.com/homeassistant-ai/ha-mcp` (category: Integration) > **Download**. Or copy `custom_components/ha_mcp_tools/` from this repository into your HA `config/custom_components/` directory.

After installing, restart Home Assistant, then open **Settings** > **Devices & Services** > **Add Integration**, search for **HA-MCP Custom Component**, and add the **HA MCP Tools** services entry.

To run the full ha-mcp server in-process through this same component, see the **Get Started** section at the top and the [full in-process server documentation →](docs/in-process-server.md).

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

By default, the full tool catalog (~84 tools) is listed to the client through the standard MCP `tools/list` response. Clients with deferred / on-demand tool loading (Claude Sonnet, Claude Opus) handle that fine — tools are pulled into context only when needed, so idle context cost is near-zero.

For models *without* deferred tool support — Claude Haiku, Gemini, ChatGPT OpenAI-compatible local models, smaller open-weights models — listing the full tool catalog up front adds a lot of idle context and can overwhelm smaller models. To address that, the server ships with a **search-based discovery mode** built on top of FastMCP's BM25 search transform.

### Smaller or local LLMs (Ollama, etc.)

If your model can't see the tools or your Home Assistant, it may be getting handed the whole tool catalog at once and struggling with it. It's recommended to try the following to see if it helps:

- **Enable tool search** (`ENABLE_TOOL_SEARCH=true`, or the add-on option below). Instead of listing every tool up front, the server defers the catalog behind a search interface so the model pulls in only the tools it needs, when it needs them.
- **Raise the model's context window above the default.** Local runtimes ship with small defaults (Ollama's `num_ctx` is one example) that can't hold a large tool set plus the conversation — increase it well beyond the default.

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
| `ENABLE_TOOL_SEARCH` | `false` | Replace full tool catalog with search-based discovery (tools deferred behind on-demand search). |
| `TOOL_SEARCH_MAX_RESULTS` | `5` | Max results returned by `ha_search_tools` (range 2–10). |
| `PINNED_TOOLS` | empty | Comma-separated tool names to keep always visible. The web settings UI is the primary way to manage this. |

### When to enable

- **Claude Haiku, OpenAI-compatible local models, Gemini, ChatGPT or any model without native deferred tool support** — large idle-context savings.
- MCP clients that cap total tool count (some cap at 100) — surfaces a minimal set (~10 tools) instead of 84.
- **Cost-sensitive deployments** — fewer idle tokens per turn.

Leave it off when using Claude Sonnet/Opus or any client with deferred tool loading; the full catalog has no idle cost there and direct calls skip the search step. If you choose to use our toolsearch then you should disable the native Claude Opus/Sonnet toolsearch, which is called deferred tools in the settings.

> 🔄 **Refresh your client's tool list after changing this (or any) setting.** Toggling `ENABLE_TOOL_SEARCH` (or changing pinned/disabled tools, Read Only Mode, etc.) changes the tools the server exposes, but your AI client keeps serving its **cached** tool list until it re-fetches. Restarting the add-on or Home Assistant does **not** refresh the client — reconnect or refresh the MCP server in your client (e.g. re-add/refresh the connector in ChatGPT, or close and reopen Claude Desktop). If you skip this, tools shown as available will return `Unknown tool` when called.

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

- **No telemetry today** — anonymous usage stats are a planned future feature (as of June 2026); when it lands it will follow your Home Assistant analytics/telemetry setting (which you can override), announced prominently in the release notes and the web Settings UI at least one month beforehand
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
- **[@SealKan](https://github.com/SealKan)** — `fields=`/`attribute_keys=` projection on six read-heavy tools (#1225), `ha_call_event` tool (#1239), dashboards-list helper refactor (#1207), `for:`-field duration-math detector in the best-practice checker (#1264), persistent DCR OAuth client registrations across restarts (#1265), and issue-triage prompt token-budgeting (#1522).
- **[@KarelTestSpecial](https://github.com/KarelTestSpecial)** — Cached YAML instance to prevent CPU spikes during bulk edits (#1371).
- **[@corgan2222](https://github.com/corgan2222)** — HA brand assets for custom integration (#1317).
- **[@drseanwing](https://github.com/drseanwing)** — Progress emission via FastMCP `Context` in long-running tools (#1124); tool-discovery / categorized-search docs (#1123).
- **[@fnordpig](https://github.com/fnordpig)** — Config subentry support (#1393) and Assist pipeline management tool (#1392).
- **[@paul43210](https://github.com/paul43210)** — `array_patch` mode in `ha_manage_addon` for atomic GET-modify-POST (#1063).
- **[@L1AD](https://github.com/L1AD)** — Filed #966 proposing tool security policies; pointed to PolicyLayer's MCP-security work as prior art that inspired the predicate DSL shape.
- **[@nightcityblade](https://github.com/nightcityblade)** — Updated stale Home Assistant Advanced Mode references after HA 2026.6 made formerly advanced options available by default (#1533).
- **[@emmelutzer](https://github.com/emmelutzer)** — Financial support via [GitHub Sponsors](https://github.com/sponsors/julienld). Thank you! ☕
- **[@pkkr](https://github.com/pkkr)** — `ha_knx_get_project` tool exposing KNX group addresses from an uploaded ETS project file.
- **[@cbowns](https://github.com/cbowns)** — Fixed inconsistent hyphen in setup.astro Codex CLI docs.
- **[@Shaan-alpha](https://github.com/Shaan-alpha)** — Extended `ha_restart` known-good error patterns to cover 502/503 responses from reverse proxies.
- **[@rebelancap](https://github.com/rebelancap)** — Fixed UTC-to-local timezone conversion in `add_timezone_metadata`.
- **[@saevras](https://github.com/saevras)** — Fixed blueprint import E2E test to use local URL instead of host-to-container networking.
- **[@jasonjhofmann](https://github.com/jasonjhofmann)** — Recurring calendar events via `rrule` support in `ha_config_set_calendar_event`.
- **[@vpciii](https://github.com/vpciii)** — Coerce JSON-encoded strings on dict/list tool params.
- **[@pburtchaell](https://github.com/pburtchaell)** — Financial support via [GitHub Sponsors](https://github.com/sponsors/julienld). Thank you! ☕
---

## 💬 Community

- **[GitHub Discussions](https://github.com/homeassistant-ai/ha-mcp/discussions)** — Ask questions, share ideas
- **[Issue Tracker](https://github.com/homeassistant-ai/ha-mcp/issues)** — Report bugs, request features, or suggest tool behavior improvements

---

## ⭐ Star History

[![Star History Chart](https://api.star-history.com/svg?repos=homeassistant-ai/ha-mcp&type=Date)](https://star-history.com/#homeassistant-ai/ha-mcp&Date)
