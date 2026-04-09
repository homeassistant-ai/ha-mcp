# Diagnostic Tool Consolidation Design

**Date:** 2026-03-28
**Replaces:** PR #675 (8 new diagnostic tools)
**Closes:** Issue #684 (system/error/addon log access)

## Goal

Add access to 3 genuinely new HA data sources (system logs, repairs, ZHA radio metrics) by expanding existing tools rather than adding new ones. Zero net tool count increase.

## Changes

### 1. Rename `ha_get_logbook` to `ha_get_logs` — add `source` parameter

**Rationale:** The existing tool only accesses the logbook API. Renaming to `ha_get_logs` and adding a `source` parameter creates a single gateway for all log types without adding tools. Per project policy, renaming is non-breaking since the same outcomes remain achievable.

**Signature:**

```python
async def ha_get_logs(
    source: str = "logbook",       # "logbook" | "system" | "error_log" | "supervisor"
    # Shared parameters
    limit: int | str | None = None,
    search: str | None = None,
    # Logbook-specific (ignored for other sources)
    hours_back: int | str = 1,
    entity_id: str | None = None,
    end_time: str | None = None,
    offset: int | str = 0,
    # System/error_log-specific
    level: str | None = None,      # ERROR, WARNING, INFO, DEBUG
    # Supervisor-specific
    slug: str | None = None,       # Add-on slug (e.g., "core_mosquitto")
) -> dict[str, Any]
```

**Source behaviors:**

| Source | Backend API | Default Limit | Notes |
|--------|-------------|---------------|-------|
| `logbook` | `GET /api/logbook` | 50 (max 500) | Existing behavior, unchanged |
| `system` | WebSocket `system_log/list` | 50 (max 500) | Structured JSON, deduplicated, ~50 entries from HA |
| `error_log` | `GET /api/error_log` | 100 (max 500) | Raw text, returns most recent N lines |
| `supervisor` | WebSocket `supervisor/api` → `/addons/{slug}/logs` | 100 (max 500) | Requires `slug` param, returns add-on container logs |

**Validation:**
- `source="supervisor"` without `slug` → validation error with suggestion to use `ha_search_entities(domain_filter="update")` or check add-on slugs
- Invalid `source` value → validation error listing valid options
- Invalid `level` value → validation error listing valid levels

**Return structure (system source example):**

```python
{
    "success": True,
    "source": "system",
    "entries": [...],          # Structured log entries
    "total_entries": 47,
    "returned_entries": 47,
    "limit": 50,
    "filters_applied": {"level": "ERROR", "search": "zha"},
}
```

**Return structure (supervisor source example):**

```python
{
    "success": True,
    "source": "supervisor",
    "slug": "core_mosquitto",
    "log": "...",              # Raw text log output
    "total_lines": 230,
    "returned_lines": 100,
    "limit": 100,
}
```

### 2. Expand `ha_get_system_health` — add `include` parameter

**Signature:**

```python
async def ha_get_system_health(
    include: str | None = None,    # Comma-separated: "repairs", "zha_network"
) -> dict[str, Any]
```

**Behavior:**
- No `include` → existing behavior (system health info only)
- `include="repairs"` → also fetch `repairs/list_issues` via WebSocket
- `include="zha_network"` → also fetch `zha/devices` via WebSocket (LQI/RSSI metrics)
- `include="repairs,zha_network"` → fetch both

**Return structure with includes:**

```python
{
    "success": True,
    "health_info": {...},          # Existing health data
    "component_count": 12,
    # Added when include contains "repairs":
    "repairs": {
        "issues": [...],           # List of repair items
        "count": 3,
    },
    # Added when include contains "zha_network":
    "zha_network": {
        "devices": [...],          # ZHA devices with radio metrics
        "count": 15,
    },
}
```

**Error handling for includes:**
- If ZHA isn't installed, `zha_network` section returns `{"error": "ZHA integration not available", "devices": [], "count": 0}` — does not fail the whole response.
- If repairs endpoint fails, same pattern — section-level error, rest of response still returned.
- Invalid include values are silently ignored (no error for unknown keys).

### 3. Expand `ha_get_device` — auto-include ZHA radio metrics

**No new parameters.** When looking up a single ZHA device (via `device_id` or `entity_id`), automatically call `zha/devices` and merge LQI/RSSI data into the device response.

**Changes to single device response:**

```python
{
    "success": True,
    "device": {
        # ... existing fields ...
        "integration_type": "zha",
        "ieee_address": "00:11:22:33:44:55:66:77",
        # New fields (ZHA devices only):
        "radio_metrics": {
            "lqi": 255,            # Link Quality Indicator (0-255)
            "rssi": -45,           # Signal strength in dBm
        },
    },
}
```

**Behavior:**
- Only triggers for single device lookup (not list mode — too expensive to call `zha/devices` for every device listing)
- Only triggers when `integration_type == "zha"`
- If `zha/devices` call fails, omit `radio_metrics` silently (don't fail the response)
- Match ZHA device to registry device by IEEE address

### 4. Test Updates

**Existing tests to update:**
- `tests/src/e2e/tools/test_logbook.py` — rename tool calls from `ha_get_logbook` to `ha_get_logs`, add `source="logbook"` parameter
- `tests/src/e2e/workflows/system/test_system_tools.py` — update `ha_get_system_health` calls, add tests for `include` parameter
- `tests/src/e2e/workflows/registry/test_device_registry.py` — no changes needed (ZHA enrichment is opportunistic, won't affect non-ZHA test environment)
- `tests/src/e2e/utilities/wait_helpers.py` — update `ha_get_logbook` reference to `ha_get_logs`

**New tests to add:**
- `tests/src/e2e/tools/test_logs.py` (or update existing `test_logbook.py`):
  - `test_system_log_entries` — basic system log retrieval
  - `test_system_log_level_filter` — filter by severity
  - `test_system_log_search` — keyword search
  - `test_error_log_raw` — raw error log retrieval
  - `test_invalid_source` — validation error
  - `test_supervisor_logs_missing_slug` — validation error
- System health tests:
  - `test_system_health_with_repairs` — repairs include
  - `test_system_health_with_invalid_include` — graceful handling

### 5. Files Modified

| File | Change |
|------|--------|
| `src/ha_mcp/tools/tools_utility.py` | Rename `ha_get_logbook` → `ha_get_logs`, add `source` parameter and new source backends |
| `src/ha_mcp/tools/tools_system.py` | Add `include` parameter to `ha_get_system_health`, add repairs/ZHA sections |
| `src/ha_mcp/tools/tools_registry.py` | Add ZHA radio metrics enrichment to `ha_get_device` single-device path |
| `tests/src/e2e/tools/test_logbook.py` | Update tool name, add new source tests |
| `tests/src/e2e/workflows/system/test_system_tools.py` | Add include parameter tests |
| `tests/src/e2e/utilities/wait_helpers.py` | Update tool name reference |

No new files created in `src/`. Test file may be renamed from `test_logbook.py` to `test_logs.py`.
