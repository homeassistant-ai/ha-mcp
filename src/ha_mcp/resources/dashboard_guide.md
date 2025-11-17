# Home Assistant Dashboard Configuration Guide

## Critical Validation Rules

### url_path MUST contain hyphen (-)
Dashboard url_path is REJECTED without hyphen. Valid: "my-dashboard", Invalid: "mydashboard"

### Dashboard ID vs url_path
- **dashboard_id**: Internal identifier (returned on create, used for update/delete)
- **url_path**: URL identifier (user-facing, used in dashboard URLs)

## Dashboard Structure

```json
{
  "title": "Dashboard Title",
  "icon": "mdi:icon-name",
  "config": {
    "views": [...]
  }
}
```

## View Configuration

### View Types (type field)
- **sections** (default): Grid layout with grouped sections
- **masonry**: Column-based auto-layout by card size
- **panel**: Single full-width card (maps, images)
- **sidebar**: Two-column layout (wide left, narrow right)

### View Structure
```json
{
  "title": "View Name",
  "path": "unique-path",
  "type": "sections",
  "icon": "mdi:icon",
  "theme": "theme-name",
  "badges": ["sensor.entity_id"],
  "cards": [...]
}
```

**Key Properties:**
- `path`: URL identifier for deep linking
- `badges`: Entity IDs displayed at top
- `visible`: Boolean or user ID list for conditional display
- `subview`: true = hidden from navigation (requires back_path)
- `background`: Image/color background (url, opacity, size, position, repeat, attachment)

## Card Categories

**Container Cards:** vertical-stack, horizontal-stack, grid
**Logic Cards:** conditional, entity-filter
**Display Cards:** sensor, history-graph, statistics-graph, gauge, energy, webpage, calendar, logbook, clock
**Control Cards:** button, entity, entities, light, thermostat, humidifier, alarm-panel
**Hybrid Cards:** area, picture-elements, picture-glance, glance, tile, heading

## Card Configuration

### Common Card Structure
```json
{
  "type": "entity",
  "entity": "light.living_room",
  "name": "Custom Name",
  "icon": "mdi:lightbulb",
  "features": [...],
  "card_mod": {}
}
```

### Features (Quick Controls)
Available on: tile, area, humidifier, thermostat cards

**Climate Features:**
- climate-hvac-modes: {"type": "climate-hvac-modes", "style": "dropdown"}
- climate-fan-modes: {"type": "climate-fan-modes", "style": "icons"}
- climate-preset-modes: {"type": "climate-preset-modes"}
- target-temperature: {"type": "target-temperature"}

**Light Features:**
- light-brightness: {"type": "light-brightness"}
- light-color-temp: {"type": "light-color-temp"}

**Cover/Valve Features:**
- cover-open-close, cover-position, cover-tilt, cover-tilt-position
- valve-open-close, valve-position

**Fan Features:**
- fan-speed, fan-direction, fan-oscillate, fan-preset-modes

**Media Player Features:**
- media-player-playback: {"type": "media-player-playback"}
- media-player-volume-slider, media-player-volume-buttons

**Other Features:**
- toggle, button, alarm-modes, lock-commands, lock-open-door
- vacuum-commands, lawn-mower-commands, water-heater-operation-modes
- numeric-input, date, counter-actions, update-actions
- bar-gauge, trend-graph: {"type": "trend-graph", "hours_to_show": 24}

Feature `style` options: "dropdown" or "icons"

### Actions (Tap Behavior)
```json
{
  "tap_action": {"action": "toggle"},
  "hold_action": {"action": "more-info"},
  "double_tap_action": {"action": "call-service", "service": "light.turn_on"}
}
```

Action types: toggle, call-service, more-info, navigate, url, none

### Visibility Conditions
```json
{
  "visibility": [
    {"condition": "user", "users": ["user_id_hex"]},
    {"condition": "state", "entity": "sun.sun", "state": "above_horizon"}
  ]
}
```

## Strategy-Based Dashboards

Auto-generated dashboards using built-in strategies:

```json
{
  "config": {
    "strategy": {
      "type": "home",
      "favorite_entities": ["light.living_room", "climate.bedroom"]
    }
  }
}
```

**Strategy Types:**
- **home**: Default Home Assistant auto-layout
- **areas**: Area-based organization
- **map**: Map-centric dashboard

## Common Pitfalls

### Dashboard Creation
- Missing hyphen in url_path → REJECTED
- Empty config is VALID (can add views later)
- title is REQUIRED for create
- icon is OPTIONAL (default: mdi:view-dashboard)

### Entity References
Use FULL entity IDs: "light.living_room" NOT "living_room"
Verify entities exist with ha_search_entities() or ha_get_overview()

### Card Type Mismatches
Entity domain must match card type:
- light entities → light card, entity card, tile card
- climate entities → thermostat card, tile card
- sensor entities → sensor card, gauge card, entity card

### Features Compatibility
Features only work on specific cards:
- climate-* features → thermostat card, tile card (climate entity)
- light-* features → light card, tile card (light entity)
- Check card type + entity domain match

### Metadata Updates
Use ha_config_update_dashboard_metadata() for title/icon changes
Use ha_config_set_dashboard() for config changes
Requires dashboard_id NOT url_path

## Resource References

Card type documentation: `ha-dashboard://card-docs/{card-type}`
Available card types: `ha-dashboard://card-types`

Examples:
- `ha-dashboard://card-docs/light` → Light card documentation
- `ha-dashboard://card-docs/thermostat` → Thermostat card documentation
- `ha-dashboard://card-types` → List of all 41 card types
