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
