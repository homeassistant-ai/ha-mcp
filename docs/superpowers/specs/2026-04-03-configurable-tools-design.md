# User-Configurable Tools & Pinned Tools

**Issue:** [#798](https://github.com/homeassistant-ai/ha-mcp/issues/798)
**Date:** 2026-04-03

## Summary

Add per-tool enable/disable and pin/unpin configuration to the HAOS add-on UI. Uses a denylist approach — all tools enabled by default, users disable what they don't want. Replaces the `enable_yaml_config_editing` toggle with the more general mechanism.

## Config Schema

Three new fields in `homeassistant-addon/config.yaml`:

```yaml
options:
  disabled_tools:
    - "ha_config_set_yaml"
  pinned_tools: []
  tool_search_max_results: 5

schema:
  disabled_tools: [str]?
  pinned_tools: [str]?
  tool_search_max_results: "int(2,10)?"
```

**Remove:** `enable_yaml_config_editing` option and schema entry.

### disabled_tools

Accepts both **group names** and **individual tool names**. Group names are expanded server-side to all tools with that tag.

Valid group names (matching existing `tags=` on tools):
- Add-ons, Areas & Floors, Automations, Blueprints, Calendar, Camera
- Dashboards, Device Registry, Entity Registry, Files, Groups, HACS
- Helper Entities, History & Statistics, Integrations, Labels & Categories
- Scripts, Search & Discovery, Service & Device Control, System
- Todo Lists, Utilities, Zones

Default: `["ha_config_set_yaml"]` (YAML editing disabled by default, matching prior `enable_yaml_config_editing: false` behavior).

### pinned_tools

Additional tool names to pin (always visible) when tool search is active. Added on top of the mandatory pinned set (`DEFAULT_PINNED_TOOLS`). No effect when tool search is disabled.

Default: `[]`

### tool_search_max_results

Number of results returned by `ha_search_tools`. Range 2-10.

Default: `5`

## Mandatory Tools (cannot be disabled)

Server silently re-enables these if they appear in `disabled_tools`:

**Always mandatory:**
- `ha_search_entities`
- `ha_get_overview`
- `ha_get_state`
- `ha_report_issue`

**Mandatory when tool search is active:**
- `ha_search_tools` (synthetic search tool)
- `ha_call_read_tool` (synthetic proxy)
- `ha_call_write_tool` (synthetic proxy)
- `ha_call_delete_tool` (synthetic proxy)

## Implementation

### 1. Add-on config (`homeassistant-addon/`)

- `config.yaml`: Add new options/schema, remove `enable_yaml_config_editing`
- `translations/en.yaml`: Add descriptions for new fields, remove old one
- `start.py`: Read new config, export as env vars (`DISABLED_TOOLS`, `PINNED_TOOLS`, `TOOL_SEARCH_MAX_RESULTS`), remove `enable_yaml_config_editing` handling

### 2. Settings (`src/ha_mcp/config.py`)

- Add `disabled_tools: str` (comma-separated, default `"ha_config_set_yaml"`)
- Add `pinned_tools: str` (comma-separated, default `""`)
- Add `tool_search_max_results: int` (default 5, validated 2-10)
- Remove `enable_yaml_config_editing` field

### 3. Server (`src/ha_mcp/server.py`)

After `register_all_tools()` and before `_apply_tool_search()`:

1. Parse `disabled_tools` into group names and tool names
2. Call `mcp.disable(tags={group})` for each group name
3. Call `mcp.disable(names={tool_names})` for individual tools
4. Call `mcp.enable(names={mandatory_tools})` to protect mandatory tools
5. Pass `settings.pinned_tools` to `_apply_tool_search()` to merge with `DEFAULT_PINNED_TOOLS`
6. Pass `settings.tool_search_max_results` to `CategorizedSearchTransform`

### 4. YAML config tool (`src/ha_mcp/tools/tools_yaml_config.py`)

Remove the internal `enable_yaml_config_editing` feature flag check. The tool registers unconditionally — disabling is now handled by the general `mcp.disable()` mechanism.

### 5. Forward compatibility

- New tools added in future versions auto-enable (denylist approach)
- Unknown group/tool names in `disabled_tools` are silently ignored (logged as warning)
- Config doesn't break when tools are added or removed

## Group-to-Tag Mapping

The server maintains a `GROUP_NAMES` dict mapping user-facing group names to the tag strings used in `@mcp.tool(tags={"..."})`. These are identical today:

```python
TOOL_GROUPS: set[str] = {
    "Add-ons", "Areas & Floors", "Automations", "Blueprints",
    "Calendar", "Camera", "Dashboards", "Device Registry",
    "Entity Registry", "Files", "Groups", "HACS",
    "Helper Entities", "History & Statistics", "Integrations",
    "Labels & Categories", "Scripts", "Search & Discovery",
    "Service & Device Control", "System", "Todo Lists",
    "Utilities", "Zones",
}
```
