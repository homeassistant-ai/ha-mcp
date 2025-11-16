# Dashboard "Take Control" - Research Findings

## Question
How does the "Take control" feature work for auto-generated dashboards (type: "home", "areas")? Can we get the expanded config via WebSocket or is it frontend-only?

## Answer: Frontend-Only Generation

### Key Finding
**Dashboard strategies are 100% frontend code** - The expansion happens in the browser, NOT on the backend.

### How It Works

1. **Strategy-Based Dashboards**: Home Assistant supports "strategy" dashboards that are defined by a simple config like:
   ```yaml
   strategy:
     type: home
     favorite_entities: []
   ```

2. **Frontend Expansion**: When you view the dashboard, the **frontend** dynamically generates the full config by:
   - Reading entities from Home Assistant
   - Reading areas/floors
   - Organizing them into views/cards
   - This happens in TypeScript/JavaScript in the browser

3. **Take Control Process**:
   - When you click "Take Control", the frontend calls `expandLovelaceConfigStrategies()`
   - This function recursively expands all strategy references into concrete configs
   - The expanded config is then saved to backend via `lovelace/config/save` WebSocket command
   - After saving, the dashboard switches from "generated" mode to "storage" mode

### Available Strategies (Built-in)

**Dashboard Strategies:**
- `home` - Smart home overview with main view + area subviews + media players
- `areas` - Area-focused dashboard with customizable area display
- `original-states` - Legacy state-based view
- `map` - Map-based dashboard
- `iframe` - Embedded iframe dashboard

**View Strategies** (used within dashboards):
- `area` - Single area view
- `areas-overview` - All areas overview
- `home-main` - Home dashboard main view
- `home-area` - Home dashboard area subview
- `home-media-players` - Media players view
- `energy` - Energy management view
- `light` - Light control view
- `security` - Security view
- `climate` - Climate control view

**Section Strategies:**
- `common-controls` - Common control sections

### Source Code Locations

**Frontend (home-assistant/frontend):**
- `src/panels/lovelace/strategies/home/home-dashboard-strategy.ts` - Home strategy
- `src/panels/lovelace/strategies/areas/areas-dashboard-strategy.ts` - Areas strategy
- `src/panels/lovelace/strategies/get-strategy.ts` - Strategy loader/expander
- `src/panels/lovelace/editor/hui-dialog-save-config.ts` - Take control dialog

**Backend (home-assistant/core):**
- `homeassistant/components/lovelace/` - Only handles storage, NOT generation
- Backend returns `{"mode": "auto-gen"}` when no config exists
- Backend has NO code to expand strategies

### Example: Home Strategy Generation

```typescript
// Simplified from home-dashboard-strategy.ts
static async generate(config, hass) {
  const areas = getAreas(hass.areas);

  // Create area subviews
  const areaViews = areas.map(area => ({
    title: area.name,
    path: `areas-${area.area_id}`,
    subview: true,
    strategy: {
      type: "home-area",
      area: area.area_id
    }
  }));

  return {
    views: [
      {
        icon: "mdi:home",
        path: "home",
        strategy: {
          type: "home-main",
          favorite_entities: config.favorite_entities
        }
      },
      ...areaViews,
      // media players view
    ]
  };
}
```

## Implications for MCP Implementation

### ❌ Cannot Be Done Via Backend

We **CANNOT** get the expanded "home" or "areas" dashboard config via WebSocket because:

1. The expansion logic lives in frontend TypeScript
2. Backend has no knowledge of how to expand strategies
3. `expandLovelaceConfigStrategies()` function requires frontend context (browser DOM, loaded modules)

### ✅ What We CAN Do

**Option 1: Save Strategy-Based Dashboards**
```python
# Create dashboard with strategy reference
ha_config_create_dashboard(
    url_path="my-home-dashboard",
    title="My Home",
    initial_config={
        "strategy": {
            "type": "home",
            "favorite_entities": ["light.bedroom", "climate.living_room"]
        }
    }
)
```

When users view this dashboard in browser, it will auto-generate like the default dashboard.

**Option 2: Recommend Frontend Action**

When users want a "home-like" dashboard:
1. Tell them to open Home Assistant UI
2. Create default dashboard (which uses `type: home` strategy)
3. Click "Take Control" button
4. Use `ha_config_get_dashboard()` to fetch the now-expanded config
5. Use that as a template for AI modifications

**Option 3: Document the Limitation**

Add to tool documentation:
```
Note: Auto-generated dashboards (type: "home", "areas") are expanded
by the frontend. To get a copy of an auto-generated dashboard:
1. Open the dashboard in Home Assistant UI
2. Click the edit icon, then "Take Control"
3. Use ha_config_get_dashboard() to retrieve the expanded config
```

### Strategy Config Format

If we want to support creating strategy-based dashboards:

```python
# Dashboard-level strategy
config = {
    "strategy": {
        "type": "home",  # or "areas", "map", etc.
        "favorite_entities": ["light.bedroom"]  # strategy-specific options
    }
}

# View-level strategy (within a dashboard)
view = {
    "title": "Kitchen",
    "path": "kitchen",
    "strategy": {
        "type": "area",
        "area": "kitchen"
    }
}
```

## Updated Implementation Recommendation

### Add Strategy Support to Tools

Update `ha_config_create_dashboard` documentation:

```python
@mcp.tool
async def ha_config_create_dashboard(..., initial_config=None):
    """
    Create a new Home Assistant dashboard.

    The initial_config can be either:

    1. Strategy-based (auto-generated by frontend):
       {
           "strategy": {
               "type": "home",  # or "areas", "map"
               "favorite_entities": ["light.bedroom"]
           }
       }

    2. Manual config (full control):
       {
           "views": [{
               "title": "Home",
               "cards": [...]
           }]
       }

    Note: Strategy dashboards are expanded by the frontend. To get an
    expanded version for modification:
    1. View dashboard in UI and click "Take Control"
    2. Use ha_config_get_dashboard() to retrieve expanded config

    Available strategies: home, areas, map, original-states, iframe
    """
```

### Add Helper Tool (Optional)

```python
@mcp.tool
async def ha_config_create_strategy_dashboard(
    url_path: str,
    title: str,
    strategy_type: Literal["home", "areas", "map"],
    strategy_options: dict | None = None
):
    """
    Create a dashboard using a built-in Home Assistant strategy.

    Strategies are templates that auto-generate dashboard content based
    on your entities, areas, and configuration. The frontend dynamically
    creates views and cards.

    EXAMPLES:

    Create home-style dashboard:
    ha_config_create_strategy_dashboard(
        url_path="my-home",
        title="My Home",
        strategy_type="home",
        strategy_options={"favorite_entities": ["light.bedroom"]}
    )

    Create area-focused dashboard:
    ha_config_create_strategy_dashboard(
        url_path="areas-view",
        title="Areas",
        strategy_type="areas",
        strategy_options={
            "areas_display": {
                "hidden": ["garage"],
                "order": ["kitchen", "bedroom", "living_room"]
            }
        }
    )
    """
    config = {"strategy": {"type": strategy_type, **(strategy_options or {})}}
    return await ha_config_create_dashboard(
        url_path=url_path,
        title=title,
        initial_config=config
    )
```

## Summary

### What We Learned

1. ✅ **Strategies are frontend-only** - TypeScript code in browser
2. ✅ **Backend doesn't expand** - Only stores/retrieves configs
3. ✅ **"Take Control" works by**:
   - Frontend calls `expandLovelaceConfigStrategies()`
   - Expanded config saved via WebSocket `lovelace/config/save`
   - Mode switches from "generated" to "storage"
4. ✅ **We can create strategy dashboards** - Just reference the strategy type
5. ❌ **We cannot get expanded config** - Without browser frontend

### For Issue #67

The original implementation plan is still valid:
- ✅ Can create/update/delete dashboards
- ✅ Can get/set dashboard configs
- ✅ Can create strategy-based dashboards
- ❌ Cannot expand strategies (need frontend for that)

**Recommendation**: Document that users should use "Take Control" in UI first if they want to work with expanded strategy configs.
