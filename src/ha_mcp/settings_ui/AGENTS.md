# Settings UI

The web-based settings page served at **`/mcp/settings`** (under the MCP path prefix, NOT `/settings`). Lets users enable/disable/pin MCP tools, edit server settings, manage backups, configure tool-security policies, and pick accessibility/theme options. Works across all install methods (add-on, Docker, standalone).

## Design goal: mimic the Home Assistant frontend

The page should look like it belongs **inside Home Assistant**, NOT like the ha-mcp docs site. Match HA's design language: the `#03a9f4` primary, the dark blue-grey app toolbar, Roboto, Material cards, and — critically — HA's own component shapes. Switches replicate `ha-switch` (a 48×24 track with an 18px thumb; tokens `--switch-*` in `settings.css`); radios/checkboxes are restyled with `appearance: none` to Material circles/boxes rather than browser defaults. When unsure of a component's exact geometry/tokens, read the real source in `home-assistant/frontend` (e.g. `src/components/ha-switch.ts`) — the upstream HA frontend is the source of truth, so don't replicate from memory.

This is unrelated to the anti-FOUC parity constraint below: that couples only the theme-*preference-resolution JavaScript* to the docs site, not the visual design. Restyling components freely is fine and does not break that test.

## Running it locally (to see the UI)

Use the **`ha-mcp-web`** entrypoint (HTTP mode). Do NOT use plain `ha-mcp` — that defaults to **stdio** transport and exits immediately with "Stdin Not Available" when there's no interactive stdin. The settings routes are only registered on the HTTP server.

```bash
HOMEASSISTANT_URL=http://<your-ha>:8123 \
HOMEASSISTANT_TOKEN=$HA_TOKEN \
MCP_PORT=8086 \
uv run ha-mcp-web
```

Then open **`http://localhost:8086/mcp/settings`**.

Two URL gotchas that cost time:

- **The path is `/mcp/settings`, not `/settings`.** The settings routes live under the MCP path prefix (`MCP_SECRET_PATH`, default `/mcp`). A bare `/settings` returns 404.
- **The port is `8086` by default** (`_get_http_runtime` default; override with `MCP_PORT`).

Any reachable HA instance works for `HOMEASSISTANT_URL` / `HOMEASSISTANT_TOKEN`. For a throwaway HA with the seeded test token, see the containerized setup in `tests/AGENTS.md`. After any edit to `__init__.py`, `settings.html`, `settings.js`, or `settings.css`, **restart the process** (assets are cached at import — see Gotchas).

## Files

- `__init__.py` — loads the three assets below at import, renders the page (`_render_settings_html`), assembles the handler dict (`build_settings_handlers`), and registers routes (`register_settings_routes` / `_ingress_only` / `get_http_settings_prefix`). Re-exports the leaf/handler-module public names so `from ha_mcp.settings_ui import X` and existing test patch targets keep working.
- **Leaf modules** (no imports from the `settings_ui` package, so handler modules and `__init__` depend on them without cycles): `_persistence.py` (tool-config / metadata-cache / backup-override file I/O + `_atomic_write_json` + the override-file lock), `_supervisor.py` (add-on options merge/post + self-restart), `_theme.py` (theme-prefs load/sanitize + `theme_prefs_path()`), `_tools_meta.py` (`ToolStub` / `MANDATORY_TOOLS` / `FEATURE_GATED_TOOLS` / `_get_tool_metadata` / `apply_tool_visibility`).
- **Handler-family modules**, each exposing a `build_*_handlers(server, ...)` factory that `build_settings_handlers` merges into the handler dict: `_handlers_theme.py`, `_handlers_tools.py`, `_handlers_backups.py` (snapshots + auto-backup config), `_handlers_server.py` (restart / settings-info / feature flags; also owns the `_PROCESS_*` identity globals), `_handlers_advanced.py`. **The factory sums the C901 complexity of every nested handler**, so handlers are module-level `async def _h(server, request)` functions (each with its own C901 budget) and the factory returns thin request-only wrappers that bind `server`.
- **Cross-module patch targets:** a helper's single canonical patch site is `ha_mcp.settings_ui._<module>.<name>` (e.g. `_supervisor._supervisor_merge_and_post_options`, `_persistence._get_config_path`, `_theme.get_data_dir`). Cross-module callers reach helpers via `_<module>.<name>` attribute access so one patch reaches every caller; tests and `tools_dev.py` patch/import at that canonical site.
- `settings.html` — the page markup. Carries three substitution markers: `__HA_MCP_CSS__` and `__HA_MCP_JS__` (filled once at import with the css/js below, inside `<style>`/`<script>`) and `__HA_MCP_THEME_PREFS__` (filled per-request in `_render_settings_html()`). All three are asserted present at import; a renamed marker fails fast.
- `settings.js` — the client script, injected into `<script>__HA_MCP_JS__</script>`. Not served as a separate asset.
- `settings.css` — the stylesheet, injected into `<style>__HA_MCP_CSS__</style>`.

## Gotchas (read before editing)

- **Assets are read once, at import time** into module globals (`_SETTINGS_HTML_PATH` / `_SETTINGS_JS_PATH` / `_SETTINGS_CSS_PATH` via `Path(__file__).parent`). A change to `settings.html`, `settings.js`, or `settings.css` does **not** hot-reload — **restart the server** to see it. (Editing API/handler Python is the same: restart.)
- **Assets must sit beside `__init__.py`.** The loader resolves them relative to `__file__`. If you move them, update `Path(__file__).parent / ...`, plus `pyproject.toml` `[tool.setuptools.package-data]`, `MANIFEST.in`, and `packaging/binary/ha-mcp.spec` (its `datas` loop) — all of which list `settings_ui/settings.html`, `settings_ui/settings.js`, and `settings_ui/settings.css`. `tests/src/unit/test_resources.py` enforces the packaging entries.
- **Relative imports are double-dot.** This is a subpackage of `ha_mcp`, so siblings are `from ..config import ...`, `from ..tools.X import ...`, etc. A single dot resolves inside `settings_ui/` and will `ModuleNotFoundError`.
- **Python ↔ JS sentinel sync.** `__init__.py` substitutes `__HA_MCP_DEFAULT_PINNED__` and `__HA_MCP_MANDATORY__` into `settings.js` at load. The presence of both sentinels is asserted at import — a rename in one place that isn't mirrored in the other fails fast (not silently).
- **Anti-FOUC parity with the docs site (JS logic only, NOT visual design).** The theme/accessibility resolver core in `settings.js` (PREFS / PRESETS / apply functions / custom-color layering) must stay logically identical to `site/src/layouts/Layout.astro`. Enforced by `tests/src/unit/test_anti_fouc_parity.py`. Mirror any change in both, or that test fails. This is about *how a saved theme preference is applied before paint*, not about what the page looks like — the visual target is HA (see "Design goal").
- **Theme prefs persist server-side** (`theme_prefs.json`) because the stdio sidecar respawns on a random port (a fresh origin with empty `localStorage`). The page seeds only *missing* `localStorage` keys from the server payload; the browser's own latest choice wins.
- **Callouts use the `ha-alert` style.** Notice bars (`.ha-alert`, `.readonly-notice`, `.pin-notice`, `.restart-notice`) replicate HA's `ha-alert`: full type-tint background, rounded, leading mdi icon (`--icon-info` / `--icon-warning` masks), icon in an absolute left gutter so text flows full-width. Use this for new callouts, not a left-border bar.

## Tool capability badges

Each tool row shows a capability tier badge: **read-only → writes → deletes**. The tier comes from `categorize_capability()` in `../transforms/categorized_search.py` — the same classifier that routes `ha_call_read_tool` / `ha_call_write_tool` / `ha_call_delete_tool`, so the badge and the proxy routing never disagree. It derives from the MCP `readOnlyHint` / `destructiveHint` annotations, then splits the destructive set into write-vs-delete by name (`_remove_` / `_delete_`). The settings API emits a `category` field per tool; the JS just renders it.

## Tests

- `tests/src/unit/test_settings_ui.py` — route registration, endpoints, env-pinned tools.
- `tests/src/unit/test_settings_ui_js_behavior.py` — JS behavior (needs jsdom: `npm install` in `tests/js/`; skipped otherwise, but runs in CI).
- `tests/src/unit/test_settings_ui_handler_selection.py` — full-server vs sidecar handler dispatch.
- `tests/src/unit/test_anti_fouc_parity.py`, `test_theme_toggle_behavior.py`, `test_advanced_settings_coverage.py` — read `settings.js` by path; keep them pointed at `settings_ui/settings.js`.
