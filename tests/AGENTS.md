# E2E Test Infrastructure

## Custom Component (ha_mcp_tools)

- Component is installed into the Docker container by `_install_custom_component` in `src/e2e/conftest.py`
- HA's `call_service(return_response=True)` wraps results in `{"changed_states": [], "service_response": {...}}` — tools unwrap this with `result.get("service_response", result)` before returning
- `hass.async_add_executor_job` only passes positional args — use `lambda:` wrappers for calls needing kwargs (e.g., `mkdir(parents=True, exist_ok=True)`)
- HA Docker image uses `annotatedyaml` (PyYAML wrapper), NOT `ruamel.yaml` — custom components needing ruamel must declare it in `manifest.json` requirements
- Feature flags (`ENABLE_YAML_CONFIG_EDITING`, `HAMCP_ENABLE_FILESYSTEM_TOOLS`, `HAMCP_ENABLE_CUSTOM_COMPONENT_INTEGRATION`) are set in `ha_container_with_fresh_config` fixture

## Test Patterns

- Tests expecting tool **success**: use `mcp.call_tool_success()` inside `MCPAssertions` context
- Tests expecting tool **failure**: use `safe_call_tool()` directly (catches `ToolError`, returns parsed dict)
- Service availability checks should use `safe_call_tool` to probe, not `call_tool_success`

## E2E Test Patterns

**FastMCP validates required params at schema level.** Don't test for missing required params:
```python
# BAD: Fails at schema validation
await mcp.call_tool("ha_config_get_script", {})

# GOOD: Test with valid params but invalid data
await mcp.call_tool("ha_config_get_script", {"script_id": "nonexistent"})
```

**HA API uses singular field names:** `trigger` not `triggers`, `action` not `actions`.

**Poll after creating entities.** After creating an entity (automation, script, helper, etc.), HA needs time to register it. Never search/query immediately — use polling helpers from `tests/src/e2e/utilities/wait_helpers.py`:
```python
from ..utilities.wait_helpers import wait_for_tool_result

data = await wait_for_tool_result(
    mcp_client,
    tool_name="ha_deep_search",
    arguments={"query": "my_sensor", "search_types": ["automation"], "limit": 10},
    predicate=lambda d: len(d.get("automations", [])) > 0,
    description="deep search finds new automation",
)
```
Other available helpers: `wait_for_entity_state()`, `wait_for_condition()`, `wait_for_state_change()`. See `wait_helpers.py` for the full set.

**Exception handling in polling helpers.** `wait_helpers.py` catches a narrow `_POLLING_TRANSIENT_ERRORS` tuple inside retry loops; bugs like `TypeError` / `AttributeError` / `KeyError` / `AssertionError` propagate immediately. Don't broaden to `except Exception`.

## JS Behaviour Testing (`tests/js/`, `tests/src/unit/_js_harness.py`)

Every rendered `<script>` body in the repo (`src/ha_mcp/settings_ui.py`,
`src/ha_mcp/auth/consent_form.py`, every `.astro` page under `site/src/`)
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

When adding a new UI surface:
- Python-rendered HTML: register the renderer in `_js_harness.py::_PY_RENDERERS`.
- Astro page: drop the `.astro` file under `site/src/`; discovery walks automatically.
- Behavioural tests: add a `test_<surface>_js_behavior.py` module alongside the existing ones (`test_settings_ui_js_behavior.py`, `test_astro_setup_js_behavior.py`, `test_astro_tools_js_behavior.py`, `test_astro_layout_js_behavior.py`, `test_consent_form_js_behavior.py`) — one module per UI surface.
