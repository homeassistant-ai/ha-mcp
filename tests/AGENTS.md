# E2E Test Infrastructure

## Custom Component (ha_mcp_tools)

- Component is installed into the Docker container by `_install_custom_component` in `src/e2e/conftest.py`
- HA's `call_service(return_response=True)` wraps results in `{"changed_states": [], "service_response": {...}}`. Most tools unwrap it with `unwrap_service_response()` (`src/ha_mcp/tools/util_helpers.py`); `ha_call_service` instead *splits* it, projecting `changed_states` into `result` and surfacing `service_response` once at the top level (issue #2085)
- `hass.async_add_executor_job` only passes positional args — use `lambda:` wrappers for calls needing kwargs (e.g., `mkdir(parents=True, exist_ok=True)`)
- HA Docker image uses `annotatedyaml` (PyYAML wrapper), NOT `ruamel.yaml` — custom components needing ruamel must declare it in `manifest.json` requirements
- Feature flags (`ENABLE_YAML_CONFIG_EDITING`, `HAMCP_ENABLE_FILESYSTEM_TOOLS`) are set in `ha_container_with_fresh_config` fixture

## Backend Lanes and How to Gate a Test

The suite runs on several backends, and a test that only makes sense on some
of them is gated by a marker rather than a runtime `skip`. The markers and
their exact skip conditions are defined in
`src/e2e/conftest.py::pytest_collection_modifyitems` — read that docstring
before adding a gate:

| Marker | Runs on |
|---|---|
| `haos_only` | HAOS backends only (`HAOS_TEST_IMAGE_PATH` set). **Auto-applied** to everything under `src/e2e/haos_only/` — no marker needed there |
| `beta_haos_only` | HAOS beta-image lanes where OS/Supervisor/Core version expectations are configured. If any expectation is set, the test runs so partial configuration fails visibly |
| `container_only` | the testcontainer backend only (includes the container-embedded lane) |
| `embedded_only` | the embedded testcontainer backend only (`E2E_BACKEND=embedded`) — the one lane whose session container has ha-mcp installed inside the HA image. Skips everywhere else, including `haos_embedded` |
| `external_only` | anywhere the server-under-test runs IN the pytest process: plain testcontainer and HAOS external. Skips stdio, inaddon, container-embedded and HAOS-embedded, which cannot be reconfigured via test-process env / monkeypatch or reach an in-process mock. The name is historical — it does NOT mean "HAOS external only" |
| `inaddon_only` | HAOS inaddon mode only (`HAOS_TEST_MODE=inaddon`), where `is_running_in_addon()` paths are live |
| `haos_stdio_only` | HAOS stdio mode only (`HAOS_TEST_MODE=stdio`), where the installed `ha-mcp` command is exercised through a real subprocess transport |
| `haos_embedded_only` | HAOS embedded mode only (`HAOS_TEST_MODE=embedded`), where the in-process server shares Home Assistant's process and event loop — for tests whose subject is that shared loop (#2357). Skips every other lane at collection, so no VM boots just to skip |
| `not_on_embedded` / `not_on_haos_embedded` | everywhere except that lane, for tests the lane's own session backend already covers |
| `requires_tools_entry` | every lane EXCEPT the no-tools lanes (`E2E_NO_TOOLS_ENTRY=1`), where the File & YAML Tools entry is absent (#2292) |
| `no_tools_only` | the no-tools lanes only — the mirror of `requires_tools_entry` |

Pick the marker by what the test *needs*, not by where it happens to pass:
`external_only` is about needing an in-process server you can reconfigure,
`inaddon_only` about needing the addon's supervisor context. Read the skip
expressions, not the summary docstring — `external_only`'s name has misled
before (#1375 found 14 supervisor-mock tests silently skipping on every
testcontainer run).

A marker-gated test changes the per-lane skip counts that
`tests/src/e2e/basic/test_backend_dispatch_smoke.py` caps. Record the
increments in a new `tests/src/e2e/basic/skip_ceiling/pr<N>.json` fragment
(`{"reason": ..., "deltas": {lane: n}}`) rather than editing
`_SKIP_CEILING_BASELINE`; fragments from different PRs never conflict. Re-pin
the baseline only from a CI observation, deleting the fragments it absorbs.

**Two different things share the `ha_mcp_tools` name — don't conflate them:**

- **The component itself** (filesystem / registry tools) is installed on
  every lane by `_install_custom_component` EXCEPT the no-tools lanes
  (`E2E_NO_TOOLS_ENTRY=1`, see below), where the "File & YAML Tools" config
  entry is absent and the privileged services never register. A test that
  relies on tools-entry behaviour must carry `requires_tools_entry`;
  everywhere else a test may still rely on
  component-gated behaviour with no marker — e.g. a config entry's
  `unique_id`, which Home Assistant's own API never exposes on any endpoint.
  Consequence for `ha_search`: a query-driven call is served by the component's
  in-process scan, not the legacy REST path, so a test written for legacy-only
  behaviour passes *vacuously*. Naming `"dashboard"` in `search_types` no
  longer changes that — the component serves the surfaces its search command
  has while the dashboards leg serves that bucket, merged server-side
  (#2289). Two conditions still route such a whole call to legacy:
  `offset + limit` past the component's advertised `limit` ceiling (500
  default — `tests/src/e2e/tools/test_search_reference_graph.py` uses exactly
  that), and a `search_types` that leaves the component search command no
  surface once `dashboard` is stripped (e.g. `search_types=["dashboard"]`).
- **The in-process "server" config entry** of that same component (#1527) is
  the embedded backend only, seeded separately. That one IS lane-specific.

In production the component is optional (it ships via HACS), so server code
reading component-only data must degrade honestly for installs without it
rather than assume the e2e's always-present case.

## No-tools lanes (`E2E_NO_TOOLS_ENTRY=1`, #2292)

The rest of the suite always has the "HA-MCP File & YAML Tools" config entry,
so the state real users hit most — integration installed, second entry never
added — was untested. `E2E_NO_TOOLS_ENTRY=1` runs a lane without that entry.
It is orthogonal to the backend selectors, so each backend has its own shape:

| Lane | Topology |
|---|---|
| container (`E2E_BACKEND` unset) | **no component at all** — `_install_custom_component` is skipped for `ha_mcp_tools`, so neither files nor entry exist. A user who never installed the integration |
| container `embedded` | **server entry only** — component files are copied with `seed_entry=False` (no tools entry) and the in-process server entry is seeded as usual |
| HAOS `inaddon` | **no component** in effect — `remove_tools_entry_in_qcow2` drops the baked tools entry pre-boot; nothing else sets one up |
| HAOS `embedded` | **server entry only** — same pre-boot removal, and the staged (disabled) server entry is deliberately kept |

The staging lives in `conftest._prepare_testcontainer_config` (container) and
`conftest._prepare_haos_image` → `haos_runtime.remove_tools_entry_in_qcow2`
(HAOS). The qcow2 edit is offline and per-worker, like the recorder / HACS
refreshers, and raises rather than warning: a silent no-op would leave the
entry in place and the lane would re-test the ordinary topology while
reporting green.

Two markers gate on it, both dispatched from `pytest_collection_modifyitems`:

- `requires_tools_entry` — the test needs the entry, so it skips on these
  lanes. Applied module-wide to the filesystem / YAML suites.
- `no_tools_only` — the test pins what happens WITHOUT the entry, so it skips
  everywhere else. `src/e2e/workflows/filesystem/test_tools_entry_absent.py`
  is the module that carries it.

Since component 2.1.0 the server entry registers the `ha_mcp_tools/*`
WebSocket surface too (#2291), so on the two `embedded` shapes the shared
component capabilities answer while the privileged *services* stay gone —
that split is the whole point of the lanes, and `test_tools_entry_absent.py`
asserts both halves.

## Test Patterns

- Tests expecting tool **success**: use `mcp.call_tool_success()` inside `MCPAssertions` context
- Tests expecting tool **failure**: use `mcp.call_tool_failure()` inside `MCPAssertions`
  context. It rejects any result `assert_mcp_success()` would accept — including the
  tools that succeed with no `success` key (`pending_restart`, bulk-operation payloads)
  — so it genuinely proves the call failed. Prefer also passing `expected_error` to pin
  *which* failure; about half the current call sites omit it and assert on the returned
  dict themselves instead, which is equally fine.
- `safe_call_tool()` is for calls whose outcome the test does **not** assert: `finally`
  cleanup (so a cleanup failure cannot mask the real assertion) and service-availability
  probes. It swallows `ToolError` and returns a parsed dict, so using it for an expected
  failure means nothing verifies the call failed at all.

## E2E Test Patterns

**FastMCP validates required params at schema level.** Don't test for missing required params:
```python
# BAD: Fails at schema validation
await mcp.call_tool("ha_config_get_script", {})

# GOOD: Test with valid params but invalid data
await mcp.call_tool("ha_config_get_script", {"script_id": "nonexistent"})
```

**HA automation config uses plural root keys (HA 2024.10+):** `triggers`/`actions`/`conditions` (singular `trigger`/`action`/`condition` are still accepted as aliases). The tool canonicalizes to plural, so `ha_config_get_automation` returns the plural shape and `python_transform` operates on it.

**Poll after creating entities.** After creating an entity (automation, script, helper, etc.), HA needs time to register it. Never search/query immediately — use polling helpers from `tests/src/e2e/utilities/wait_helpers.py`:
```python
from ..utilities.wait_helpers import wait_for_tool_result

data = await wait_for_tool_result(
    mcp_client,
    tool_name="ha_search",
    arguments={"query": "my_sensor", "search_types": ["automation"], "limit": 10},
    predicate=lambda d: len(d.get("automations", [])) > 0,
    description="ha_search finds new automation",
)
```
Other available helpers: `wait_for_entity_state()`, `wait_for_condition()`, `wait_for_state_change()`. See `wait_helpers.py` for the full set.

**Exception handling in polling helpers.** `wait_helpers.py` catches a narrow `_POLLING_TRANSIENT_ERRORS` tuple inside retry loops; bugs like `TypeError` / `AttributeError` / `KeyError` / `AssertionError` propagate immediately. Don't broaden to `except Exception`.

## JS Behaviour Testing (`tests/js/`, `tests/src/unit/_js_harness.py`)

Every rendered `<script>` body in the repo (`src/ha_mcp/settings_ui/` — page
HTML in `settings.html`, client JS in `settings.js`;
`src/ha_mcp/auth/consent_form.py`; every `.astro` page under `site/src/`)
gets parse coverage automatically via
`tests/src/unit/test_rendered_scripts_parse.py`. The discovery walker in
`_js_harness.py::discover_script_surfaces` picks up new surfaces on its
next run — no registration needed when you add a new UI.

For behavioural tests, use the JSDOM harness:

```python
from ._js_harness import extract_script_body, run_script

script = extract_script_body(rendered_html)
result = run_script(
    script,
    initial_html="<!DOCTYPE html>...",
    fetch_map={"/api/foo": {"status": 200, "json": {...}}},
    broadcast_events=[{"channel": "ch-name", "data": {"type": "..."}}],
    invoke="await window.someExposedFn();",
)
assert result.reloads == 1
assert result.broadcasts_of_type("restart-required")
```

The harness fakes `setTimeout` / `setInterval` / `Date.now` on a virtual clock, stubs `fetch` from a URL map, captures `location.reload` via JSDOM's `jsdomError` channel, and provides a `BroadcastChannel` shim. `new Date()` / `performance.now()` continue to report wall time — only the three sources above are faked.

Astro `<script>` blocks without `define:vars` / `is:inline` are TypeScript by default — pass `language="ts"` to `run_script`. For Astro pages needing wizard data, use `extract_astro_frontmatter_vars` + `astro_vars_prelude` to inject production data.

CI installs Node + jsdom in the `unit-tests` job. Local devs without `tests/js/node_modules/` get clean skips.

**Transient UI + the fake clock:** timed UI (e.g. the save toast, ~4s auto-dismiss) is gone from `result.dom` by capture time because the virtual clock fast-forwards. Stamp state into a `data-` attribute *inside* `invoke` to read it live. Avoid substring false-positives too — `"ha-toast" in result.dom` matches the always-present `#ha-toast-region`; assert the specific variant class.

**Config-dir isolation:** settings / feature-flag unit tests read the real data dir via `get_global_settings()`. A dev `ha-mcp-web` server that wrote to `~/.ha-mcp` pollutes them (e.g. a beta toggle flips `enable_beta_features`, breaking the beta-gate test). Run with `HA_MCP_CONFIG_DIR=$(mktemp -d)` to isolate — how CI stays clean.

When adding a new UI surface:
- Python-rendered HTML: register the renderer in `_js_harness.py::_PY_RENDERERS`.
- Astro page: drop the `.astro` file under `site/src/`; discovery walks automatically.
- Behavioural tests: add a `test_<surface>_js_behavior.py` module alongside the existing ones (`test_settings_ui_js_behavior.py`, `test_astro_setup_js_behavior.py`, `test_astro_tools_js_behavior.py`, `test_astro_layout_js_behavior.py`, `test_consent_form_js_behavior.py`) — one module per UI surface.
