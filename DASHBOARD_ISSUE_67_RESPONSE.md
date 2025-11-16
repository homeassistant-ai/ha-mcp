# Issue #67 - Dashboard Management Response

## Summary

I've completed comprehensive research on implementing dashboard management for ha-mcp. This addresses the feature request to allow creating and modifying Home Assistant dashboards directly through the MCP server.

## What I Found

### Home Assistant Dashboard API

Home Assistant provides a **complete WebSocket API** for dashboard management through the Lovelace component:

**Dashboard Collection Management:**
- `lovelace/dashboards/list` - List all dashboards
- `lovelace/dashboards/create` - Create new dashboard
- `lovelace/dashboards/update` - Update metadata (title, icon, permissions)
- `lovelace/dashboards/delete` - Delete dashboard

**Dashboard Content Management:**
- `lovelace/config` - Get dashboard configuration (views, cards)
- `lovelace/config/save` - Save dashboard configuration
- `lovelace/config/delete` - Delete configuration

### How It Works

Dashboards have two parts:
1. **Metadata** - Title, icon, URL path, permissions (sidebar visibility, admin-only)
2. **Content** - Views, cards, layout configuration (the actual dashboard UI)

This matches Home Assistant's UI where you:
- Create a dashboard in settings (sets metadata)
- Edit the dashboard to add views/cards (sets content)

## Proposed Solution

I've created a detailed implementation proposal that adds **6 new MCP tools**:

1. **ha_config_list_dashboards()** - List all dashboards
2. **ha_config_get_dashboard(url_path)** - Get complete dashboard config
3. **ha_config_create_dashboard(...)** - Create new dashboard with optional initial config
4. **ha_config_update_dashboard(url_path, config)** - Update dashboard content (views/cards)
5. **ha_config_update_dashboard_metadata(...)** - Update title, icon, permissions
6. **ha_config_delete_dashboard(dashboard_id)** - Delete dashboard

### Benefits

**Solves Your Concerns:**
- ✅ Direct injection via MCP (no copy/paste)
- ✅ Large configs sent via WebSocket (not chat messages)
- ✅ Faster partial updates (modify specific sections)
- ✅ No formatting errors (Home Assistant validates)
- ✅ Proper YAML/JSON handling

**Implementation:**
- Follows existing script/automation/helper patterns
- Comprehensive error handling with helpful suggestions
- Supports both dict and YAML string formats
- AI-optimized with clear documentation

## Example Usage

```python
# Create a new dashboard
ha_config_create_dashboard(
    url_path="mobile-dashboard",
    title="Mobile View",
    icon="mdi:cellphone",
    initial_config={
        "views": [{
            "title": "Home",
            "cards": [
                {"type": "weather-forecast", "entity": "weather.home"},
                {"type": "entities", "entities": ["light.bedroom"]}
            ]
        }]
    }
)

# Update just the title
ha_config_update_dashboard_metadata(
    dashboard_id="mobile-dashboard",
    title="Mobile View v2"
)

# Update the content
ha_config_update_dashboard(
    url_path="mobile-dashboard",
    config={
        "views": [...]  # New views configuration
    }
)
```

## Files Created

- **DASHBOARD_IMPLEMENTATION_PROPOSAL.md** - Complete technical implementation plan
  - WebSocket client methods
  - MCP tool definitions with full docstrings
  - Testing strategy
  - Integration points

## Next Steps

If this approach looks good, I can:

1. Implement the WebSocket client methods
2. Create the tools module
3. Add comprehensive E2E tests
4. Update documentation

## Questions for You

1. Does this approach meet your needs?
2. Should we support YAML string format in addition to dict/JSON?
3. Any specific dashboard features you need (resources, themes, etc.)?

## Testing Offer

You mentioned being willing to test - that would be great! Once implemented, you could help validate:
- Large dashboard configs (hundreds of lines)
- Performance vs current copy/paste approach
- Edge cases with complex card configurations
- Windows environment testing

Let me know what you think!
