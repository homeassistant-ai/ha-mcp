# Beta Features

Some ha-mcp tools are gated behind feature flags and disabled by default. They can be enabled via the web Settings UI or via environment variables (non-add-on installs). Beta tools are still being evaluated and may change, be promoted to stable, or be removed based on field experience.

## Current beta tools

| Tool | Toggle / env var | Description |
|---|---|---|
| `ha_config_set_yaml` | `enable_yaml_config_editing` (dev add-on Configuration tab); or web Settings UI master + sub-toggle; or `ENABLE_BETA_FEATURES=true` + `ENABLE_YAML_CONFIG_EDITING=true` env vars | Raw YAML editing of `configuration.yaml` and packages/*.yaml for YAML-only integrations. |
| `ha_list_files` | `enable_filesystem_tools` (dev add-on); or web Settings UI master + sub-toggle; or `ENABLE_BETA_FEATURES=true` + `HAMCP_ENABLE_FILESYSTEM_TOOLS=true` env vars | List files in allowed directories. Requires `ha_mcp_tools` custom component. |
| `ha_read_file` | `enable_filesystem_tools` (dev add-on); or web Settings UI master + sub-toggle; or `ENABLE_BETA_FEATURES=true` + `HAMCP_ENABLE_FILESYSTEM_TOOLS=true` env vars | Read files from allowed paths. Requires `ha_mcp_tools` custom component. |
| `ha_write_file` | `enable_filesystem_tools` (dev add-on); or web Settings UI master + sub-toggle; or `ENABLE_BETA_FEATURES=true` + `HAMCP_ENABLE_FILESYSTEM_TOOLS=true` env vars | Write files to allowed directories. Requires `ha_mcp_tools` custom component. |
| `ha_delete_file` | `enable_filesystem_tools` (dev add-on); or web Settings UI master + sub-toggle; or `ENABLE_BETA_FEATURES=true` + `HAMCP_ENABLE_FILESYSTEM_TOOLS=true` env vars | Delete files from allowed directories. Requires `ha_mcp_tools` custom component. |
| `ha_manage_custom_tool` | `enable_code_mode` (dev add-on); or web Settings UI master + sub-toggle; or `ENABLE_BETA_FEATURES=true` + `ENABLE_CODE_MODE=true` env vars | Sandboxed Python "escape hatch" that lets AI assistants write, run, save, and delete custom tools when no built-in tool covers the request. Code runs in pydantic-monty (no filesystem, no network); sandbox can call the HA REST API (`api_get`/`api_post`), send WebSocket commands (`ws_send`), call registered MCP tools (`call_tool`), or delete a saved tool (`delete_saved_tool`). Saved tools persist to disk via `CODE_MODE_SAVED_TOOLS_PATH` (defaults to `/data/saved_tools.json` in the dev add-on). |
| _(behaviour flag, no new tool)_ | `enable_lite_docstrings` (dev add-on); or web Settings UI master + sub-toggle; or `ENABLE_BETA_FEATURES=true` + `ENABLE_LITE_DOCSTRINGS=true` env vars | Replaces the docstrings on a handful of heavy ha-mcp tools (automations, scripts, scenes, helpers, dashboards, `ha_call_service`, `ha_config_set_yaml`) with shorter variants that defer schema and example detail to `ha_get_skill_guide` (or its `skill://` resource). Reduces idle catalog token usage; relies on the LLM actually calling the skill tool/resource when it needs detail. See "Known limitations" below. |
| `ha_get_dashboard_screenshot` (+ `include_screenshot` on `ha_config_get_dashboard`, `return_screenshot` on `ha_config_set_dashboard`) | `enable_dashboard_screenshot` (dev add-on); or web Settings UI master + sub-toggle; or `ENABLE_BETA_FEATURES=true` + `HAMCP_ENABLE_DASHBOARD_SCREENSHOT=true` env vars | Render one or more responsive Lovelace images so the AI can see what it reads or creates. Rendering runs in a separate, opt-in engine — balloob's **Puppet** add-on (headless Chromium, Apache-2.0), which you install yourself — nothing heavy is installed unless you enable this AND install the engine. |

## How to enable

There are three paths depending on how you run ha-mcp:

### Dev channel add-on (Home Assistant users)

The dev channel add-on continues to expose its beta sub-flags on its
Configuration page. Toggle them there, restart the add-on, done — the
master beta toggle is auto-enabled for dev add-on users at start-up.
No web-UI step is needed.

### Stable channel add-on (Home Assistant users)

The stable add-on does NOT expose individual beta toggles. Use the web
Settings UI instead:

1. Open the add-on's web UI tab.
2. Open the **Server Settings** tab.
3. Flip **Enable beta features** on.
4. Enable the desired beta toggle below it (e.g. **Enable YAML config
   editing (beta)**).
5. Restart the add-on.

### Non-add-on installs (Docker, uvx, pip)

Either flip the master + sub-toggles in the web Settings UI as above, or
set both env vars: `ENABLE_BETA_FEATURES=true` AND the specific sub-flag
env var (e.g. `ENABLE_YAML_CONFIG_EDITING=true`). Sub-flag env vars are
ignored unless the master is also true.

## Known limitations

### `ha_config_set_yaml`

This tool edits `configuration.yaml` and package files directly, bypassing Home Assistant's config-entry flow. It includes safeguards (backup before every edit, YAML validation, key allowlist, path traversal blocking, post-edit config check), but operators should be aware of the following:

**Two-step confirm flow (on by default).** Because a YAML edit re-serializes the whole file, an edit can change lines the AI never intended to touch (issue #1720). To surface that before it reaches disk, the confirm flow is **on by default**: the first `ha_config_set_yaml` call writes *nothing* and returns `preview: true`, a `confirm_token`, and a unified `diff` of exactly what would change on disk. The AI reviews that diff for collateral changes, then repeats the identical call adding `confirm_token` to apply the edit; every applied write also returns the final `diff`. If the file changed between preview and confirm the token no longer matches — the tool re-previews (`confirm_token_mismatch: true`) with a fresh token instead of writing stale content. Toggle it off (single-call writes, one less round-trip) via the **Require confirmation for YAML edits** sub-toggle under YAML config editing in the web Settings UI, the dev add-on Configuration tab, or `ENABLE_YAML_EDIT_CONFIRM=false`.

**Config check has blind spots.** HA's config check — run automatically post-edit, and available manually via `ha_get_system_health(include="config_check")` — validates YAML syntax but does not catch all integration-level schema errors. An edit can pass validation, HA boots cleanly, but the target entity silently does not exist. Common LLM mistakes include mixing legacy and modern template sensor syntax, wrong field names (`value_template:` vs `state:`), and bad Jinja expressions.

**`action: remove` removes the entire top-level key.** Asking an LLM to remove a single sensor can result in the entire `template:` key being deleted, not just the intended entry.

**Most keys require a full HA restart.** Only `template`, `mqtt`, and `group` support reload. All other keys require restarting Home Assistant for changes to take effect. The tool response includes `post_action` indicating which is needed.

**`command_line:` entries execute shell commands.** The allowlist includes `command_line:` for legitimate use cases, but an LLM could inadvertently create a sensor with a command that reads sensitive files or modifies the system.

**The key allowlist is extensible per install.** Some integrations are legitimately YAML-first and too install-specific to hardcode globally. **Extra YAML write keys** (web Settings UI, nested under YAML config editing, or `HA_MCP_EXTRA_YAML_KEYS`) takes a comma-separated list of extra top-level keys `ha_config_set_yaml` may write on this install, e.g. `alert2`. Everything else is unchanged: the file allowlist, the confirm flow, the backup, and the post-edit config check all still apply, and an extra key with no reload service gets the conservative full-restart path. A small set of keys can never be added to this setting, because they redefine Home Assistant's own trust boundary rather than one integration's config: `homeassistant:` (auth providers, and the `packages:` root that bounds this very surface), `http:` (trusted proxies, CORS, IP-ban), `frontend:` and `lovelace:` (both load JavaScript modules into the authenticated dashboard). Those stay refused with an explicit message. This bounds the per-key edit path; `action="replace_file"` rewrites a whole file and has never been key-validated. Requires custom component 1.2.3 or newer.

**Recovery requires filesystem access.** If an edit causes HA to enter recovery mode (e.g., a bad `!include` reference), `ha_config_set_yaml` cannot fix its own damage since the custom component doesn't load in recovery mode. Recovery requires SSH, the File Editor add-on, or `docker exec`.

**Per-edit backups are restorable via `ha_manage_backup`.** Per-edit auto-backups are written to `.ha_mcp_tools_backups/` (at the Home Assistant config root) and can be listed, viewed, restored, and deleted with `ha_manage_backup(scope="edits", ...)`. Full HA snapshot tarballs are separate — create, list, and restore them with `scope="snapshot"`.

**Recommended prerequisites:**
- Comfort with editing `configuration.yaml` via SSH or File Editor when things go wrong
- Understanding that dedicated tools (`ha_config_set_helper`, `ha_config_set_automation`, `ha_config_set_script`, etc.) should be preferred for anything they support

### `ha_list_files`, `ha_read_file`, `ha_write_file`, `ha_delete_file`

These tools provide direct file access to your Home Assistant filesystem and require `HAMCP_ENABLE_FILESYSTEM_TOOLS=true` and the `ha_mcp_tools` custom component installed and active. Install the component through HACS (custom repository `homeassistant-ai/ha-mcp-integration`); an AI agent can drive that install with the generic HACS tools.

**Access is restricted but sensitive.** The built-in writable directories are `www/`, `themes/`, `custom_templates/`, and `dashboards/`; `ha_read_file` additionally allows reading config YAML files, logs, and `custom_components/`. Power users can grant further read+write access to extra config-relative directories or the HAOS sibling volumes (`/share`, `/media`, `/ssl`, `/backup`) via the web Settings UI — each is opt-in and enforced by the `ha_mcp_tools` component. An AI assistant with these tools enabled has meaningful read and write access to your HA configuration.

**No undo.** `ha_delete_file` and `ha_write_file` (with `overwrite=True`) are irreversible. There is no recycle bin or automatic backup for file operations.

**Requires the custom component.** If `ha_mcp_tools` is not installed and active, all file tools will return an error with installation instructions.

### `ha_manage_custom_tool`

This tool exposes a sandboxed Python interpreter (`pydantic-monty`) to the AI as an escape hatch for operations no built-in tool covers. It also lets the AI save tools for reuse via `save_as` / `run_saved` / `list_saved`, and delete them from inside the sandbox via `delete_saved_tool(name)`. Sandbox code can hit the HA REST API directly (`api_get`/`api_post`), send HA WebSocket commands (`ws_send`), or call other registered MCP tools (`call_tool`). The sandbox blocks filesystem and arbitrary network I/O, but operators should still be aware of the following:

**The AI gets to write and run code on your HA instance.** Even though the sandbox prevents it from touching the filesystem or the public network, code can still call any tool the MCP server has registered, including write/destructive tools, and can hit any endpoint reachable via the HA REST or WebSocket API. The WebSocket surface in particular covers most registry CRUD (areas, devices, entities, automations) and template rendering — so this is effectively "do whatever HA's own UI can do, in any combination." Treat this like giving the AI a generic "do whatever existing tools allow you to do, in any combination" capability — not a tightly scoped per-feature tool.

**Saved tools persist by default in the dev add-on.** Tools the AI saves via `save_as` are written to `CODE_MODE_SAVED_TOOLS_PATH` (defaults to `/data/saved_tools.json` in the add-on) and re-loaded on the next start. The cap is 256 saved tools per instance. Operators who want a clean slate can stop the add-on and delete the file. Operators migrating between environments can copy that JSON file to the new instance — it survives add-on updates, but **not** add-on uninstall/reinstall (the `/data` volume is recreated). Outside the add-on (pip / uvx / Docker direct), persistence is opt-in: set `CODE_MODE_SAVED_TOOLS_PATH=/path/to/tools.json` to enable.

**Recursive self-call is blocked, but composition is not.** The sandbox refuses to invoke `ha_manage_custom_tool` from inside itself, so it can't directly recurse, but it can chain together every other tool the server registers. A buggy or adversarial prompt can still cause unexpected fan-out across destructive tools.

**Resource limits are best-effort.** 30s wall-clock, 10 MB memory, recursion depth 100, and 100 API/tool calls per execution are enforced by the sandbox runtime; the per-execution call cap is enforced by ha-mcp itself. All four are configurable via the `CODE_MODE_MAX_*` env vars within the bounds defined in `src/ha_mcp/config.py`. They protect against runaway loops, not against intentionally crafted abuse — keep `ENABLE_CODE_MODE=false` in any environment where untrusted prompts can reach the server.

**Outbound HTTP is restricted to your HA instance.** `api_get` / `api_post` reject absolute URLs (`http://...`, `https://...`), protocol-relative URLs (`//host/...`), and userinfo (`user@host/...`). This stops a prompt-injected LLM from redirecting the request elsewhere and exfiltrating the HA bearer token via the still-attached `Authorization` header. Only HA-relative paths reach the underlying httpx client.

**Safer-path enforcement on REST and WebSocket.** Several endpoints have wrapping MCP tools that perform validation, lint, hash-locking, or invariant checks; raw `api_post` / `ws_send` would skip those. The sandbox blocks a small denylist on each surface:

- `api_post`: writes to `/api/states/<entity_id>` (which can conjure ghost entities), `/api/events/<HA-internal-event-name>` (Core internal events that can fan out into user automations), and `/api/config/{automation,script}/config/*` (forced through `ha_config_set_automation` / `ha_config_set_script`). `config/scene/config/*` is intentionally not blocked because no `ha_config_set_scene` wrapping tool exists yet — the block would just remove capability with no validated alternative path.
- `ws_send`: `config/core/update` (rewrites HA's location/timezone/currency in `.storage/core.config`), `lovelace/config/save` and `lovelace/dashboards/{create,delete,update}` (forced through `ha_config_set_dashboard`), and `config/{area,device,entity}_registry/{delete,disable,update}` (forced through `ha_set_area_or_floor` / `ha_set_device` / `ha_set_entity` etc.).
- Service calls (`POST /api/services/<domain>/<service>`), webhook firing (`POST /api/webhook/<id>`), custom event types (`POST /api/events/my_event_name`), and registry **read** queries (e.g. `config/area_registry/list`) all stay allowed.

**Sandbox failures are classified.** When sandboxed code raises, the error response now uses one of three codes — `SANDBOX_LIMIT_EXCEEDED` (memory / time / recursion / invocation cap), `SANDBOX_SYNTAX_UNSUPPORTED` (imports, classes, `with`, `match`, hard syntax errors) or `SANDBOX_RUNTIME_ERROR` (everything else) — with suggestions tailored to the category. Previously every Monty failure surfaced as `INTERNAL_ERROR` with "check the Python code for syntax errors" advice, which actively misled callers when the real cause was a memory cap or a missing module import.

**Sandbox actions are auditable.** Every state-changing sandbox call (`POST /api/...`, every `ws_send`) logs a structured `sandbox.api_post` / `sandbox.ws_send` line at DEBUG level. Blocked attempts (e.g. a refused `POST /api/states/...` or a refused `config/core/update`) log a `sandbox.api_post.blocked` / `sandbox.ws_send.blocked` line at INFO level so they're visible in default operator logs.

To get a full forensic trail of allowed calls, escalate the `ha_mcp.tools.tools_code` logger to DEBUG. This is HA's [`logger:` integration](https://www.home-assistant.io/integrations/logger/) and goes in **`configuration.yaml`** (not the add-on options):

```yaml
# configuration.yaml
logger:
  default: warning
  logs:
    ha_mcp.tools.tools_code: debug
```

Reload the `Logger` integration (or restart HA) to apply.

**ARM platforms require the async sandbox path.** On systems where `Monty.run_async` is unavailable, the tool fails fast with a clear error rather than falling back silently.

**Recommended prerequisites:**
- You're comfortable with the AI authoring small Python snippets that wrap existing tools or HA REST endpoints
- You have `destructiveHint=True` confirmation enabled on the MCP client and you actually read the prompts

### `enable_lite_docstrings`

Replaces the docstrings on a handful of heavy ha-mcp tools (automations, scripts, scenes, helpers, dashboards, `ha_call_service`, `ha_config_set_yaml`) with shorter variants that defer schema and example detail to `ha_get_skill_guide` (or its `skill://` resource). This is a behaviour flag, not a new tool.

**The trade-off.** This reduces idle tool-catalog token usage but relies on the LLM actually calling the skill tool (or reading the skill resource) when it needs detail. Some models will skip the extra tool call and produce worse output than they would have with the full docstrings in front of them.

**Search discoverability shrinks too.** Clients that BM25-search the tool catalog (claude.ai's native deferred-tool search, ha-mcp's own `enable_tool_search` index) see fewer tokens per lite-mapped tool, so natural-language queries that previously matched on words in the full docstring may rank lower or miss. ha-mcp's explicit `_SEARCH_KEYWORDS` boosts still apply on top of the lite text, but coverage outside those boosts will be thinner.

Pair this toggle with one of the following to mitigate:

- A client that supports MCP resources (the model can read `skill://` directly without an extra tool round-trip).
- `enable_tool_search` — the search transform's description already nudges the LLM toward the skill resources.
- A clear system prompt that instructs the LLM to consult `ha_get_skill_guide` before creating/editing automations, scripts, or helpers.

**Startup warning.** When this flag is enabled via environment variable, a single WARNING line is emitted at startup so non-add-on users see the trade-off in their logs. The add-on UI surfaces the same warning in the toggle's description.

**What is replaced.** Only the descriptions exposed via the MCP `list_tools` reply are swapped. The Python docstrings in `src/ha_mcp/tools/` are unchanged — the substitution happens via a FastMCP transform installed during server initialisation. Tools not in the lite mapping pass through with their original descriptions.

**Recommended prerequisites:**
- A client that lets you watch tool-call traces, so you can see whether the LLM is actually fetching the skill content before acting
- Willingness to disable the flag if the model regresses on your prompts

### Dashboard screenshot (`ha_get_dashboard_screenshot` / `include_screenshot` / `return_screenshot`)

Rendering does not run inside ha-mcp; it runs in a separate engine — balloob's
**Puppet** add-on (headless Chromium, https://github.com/balloob/home-assistant-addons).
ha-mcp does not vendor it; you install it yourself. Operators should know:

**Setup by deployment.**
- *HA OS / Supervised:* add balloob's add-on repository
  (`https://github.com/balloob/home-assistant-addons`) under Settings >
  Add-ons > Add-on Store > Repositories, install the **Puppet** add-on, set its
  `access_token` option to a Home Assistant long-lived access token, and start
  it. ha-mcp discovers the running add-on through the Supervisor (it matches
  the `*_puppet` slug). (The assistant can do this for you end-to-end via
  `ha_manage_addon(action="add_repository", repository=...)` then
  `action="install"` / `action="start"`, but you must supply the token.)
- *Docker / Container:* run Puppet's image as a sidecar (build it from
  balloob's `puppet/` directory, with `access_token` set) and point ha-mcp at
  it with `HAMCP_DASHBOARD_SCREENSHOT_ENGINE_URL=http://<engine-host>:10000`.
- *stdio / standalone:* not supported (no place to host the engine); the tool
  returns a clear error.

**A long-lived access token is required.** Puppet authenticates by injecting a
Home Assistant long-lived access token (the `access_token` option) into the
browser. There is no token-less mode: the add-on's Supervisor token is not a
valid HA frontend credential (HA Core rejects it), and a token cannot be minted
programmatically. Create the token under Profile > Security — ideally for a
dedicated, low-privilege user, since the engine holds whatever access that
token grants. If the token is missing or invalid, Puppet lands on the login
page and (by its design) restarts; ha-mcp surfaces this as a clear "set the
engine's access token" error rather than a silent failure.

Puppet's theme and dark-mode renderer controls used to dispatch Home
Assistant's `settheme` event on every cold render, which Home Assistant
persisted on the frontend profile of the user whose token the engine runs with
— and synced to that user's real web and mobile sessions, flipping a dark-mode
user's whole UI to light on every screenshot (#1909). Recent Puppet versions
fixed that cold-render dispatch, so ha-mcp's snapshot/restore bracket around
each capture is now disabled (#1991); the guard code is retained so it can be
switched back on if a future engine regression reintroduces the write. If you
run an older Puppet build, update the add-on (or your self-hosted sidecar
image) — older engines still persist the theme selection and will keep
flipping it. A dedicated Puppet account remains a sound belt-and-suspenders
setup. Language selection is local to Puppet's browser session.

To change the Puppet engine add-on's own options (such as `keep_browser_open`)
or to restart it, use `ha_manage_addon`; the screenshot tools only render and
never modify the engine add-on's configuration.

**Puppet's HTTP listener has no inbound auth, and it publishes host port
10000.** Anyone who can reach `http://<ha-host>:10000` can pull
fully-authenticated dashboard renders. Keep it on a trusted network only — do
NOT expose port 10000 to an untrusted LAN or the internet. (This is balloob's
upstream packaging; the add-on's own info page calls it a prototype with "no
security.")

**Charts are best-effort.** Canvas cards (ApexCharts, mini-graph-card,
history-graph) paint after the dashboard reports "loaded". The default
render-settle is generous, but a heavy chart card may still come back blank;
raise `wait_ms` on the standalone `ha_get_dashboard_screenshot` tool for those.

**Prefer stable view addressing.** The standalone tool accepts
`dashboard_url_path` plus the view's configured `views[].path`; dashboard get
responses expose `render_paths` for every static view. The legacy raw
`dashboard_path` remains supported, but a numeric route such as `lovelace/0`
returns a warning when that view has a stable named path. Strategy dashboards
generate views at runtime and therefore expose only their dashboard base route.

**Responsive and deterministic requests.** The standalone
`ha_get_dashboard_screenshot` tool accepts the full width, height, zoom, wait,
orientation, theme, dark-mode, language, image-format, and render-timeout
controls; the dashboard get/set workflows expose only `include_screenshot` /
`return_screenshot` plus `view_path` and render at defaults. `viewport_presets`
(standalone only) renders an ordered batch using `mobile` (390x844), `tablet`
(768x1024), and `desktop` (1280x800); every image is returned as a native MCP
image block. PNG, JPEG,
WebP, and BMP are supported by Puppet and carry their matching MIME type, but
client/model support for less-common image formats varies.

Raw image responses are streamed under server safety limits of 20 MiB per
image and 40 MiB per batch before MCP base64 encoding. Oversize responses use
the distinct `IMAGE_PAYLOAD_TOO_LARGE` error class. If one viewport in a batch
fails after another succeeded, the successful native image blocks are retained.
The standalone tool reports `partial=true`; dashboard get/set workflows report
`screenshot_partial=true`; both include ordered `screenshot_failures` entries
identifying the failed preset and failure class. The call errors only when every
requested viewport fails.

The structured `screenshots` metadata binds each image to its `content_index`,
render path, viewport, engine request, local capture options, byte length, MIME
type, and SHA-256 digest. Puppet does not report whether Home Assistant accepted
or fell back from a requested theme or language, so the frontend context is
explicitly marked as not confirmed.

**Capturing below the fold.** By default the render is clipped to the viewport.
Pass `height="auto"` or the backwards-compatible `full_page=true` to ask Puppet
to size the capture to the rendered content. Stock Puppet caps auto-height at
4000 px. It does not expose scroll position, total page height, or segment
capture, so dashboards beyond that limit remain clipped; true ordered scroll
segments require an upstream engine capability. For the backwards-compatible
`full_page=true` alias only, an HTTP 400 from Puppet versions older than 2.5.0
triggers the legacy 4096 px fixed-height retry and reports
`legacy_full_page_fallback=true`; explicit `height="auto"` remains strict.

**Current engine limits.** Puppet does not expose a device-pixel-ratio control,
confirmed applied-context metadata, or segmented scrolling. ha-mcp also cannot
observe an MCP client silently dropping an image after successful server-side
serialization. There is no screenshot file/media fallback: inline native MCP
images remain the only transport, avoiding unauthenticated persisted artifacts.

**Graceful by design — with one deliberate exception.** If the feature is off,
both `include_screenshot` and `return_screenshot` return the dashboard config /
write result with a `warnings` entry. `return_screenshot` (set) also degrades a
render failure to a warning so it never breaks a write that already committed.
`include_screenshot` (get) does not commit a dashboard/config write, and the
screenshot *is* the requested payload, so a total render failure surfaces as
an error (matching the standalone `ha_get_dashboard_screenshot` tool) rather
than a warning a caller might miss. Because Puppet can persist theme/dark
preferences (and the theme-restore bracket writes frontend user data to undo
that), screenshot operations are blocked in server Read Only Mode; ordinary
dashboard get/list/search calls remain available.

**Raw rendered paths remain constrained.** `ha_get_dashboard_screenshot`
validates legacy `dashboard_path` values (rejects URLs, query strings,
fragments, `..`, and backslashes) and checks the first route segment against
Home Assistant's registered Lovelace dashboards. A raw path therefore cannot
use Puppet's independent credential to render another frontend panel.
