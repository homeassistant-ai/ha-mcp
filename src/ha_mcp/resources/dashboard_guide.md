# Home Assistant Dashboard Configuration Guide

## Modern Dashboard Best Practices

### What is a Modern Dashboard?

**Modern Home Assistant dashboards** (2024+) use:
- **Sections view type** (default) with grid-based layouts
- **Multiple views** with navigation and deep linking
- **Grid cards** for organizing content into columns
- **Tile cards** with integrated features for quick controls
- **Navigation paths** between views for hierarchical organization

**Legacy patterns to avoid:**
- Single-view dashboards with all cards in one long scroll
- Excessive use of vertical-stack/horizontal-stack instead of grid
- Masonry view (auto-layout) - use sections for precise control
- Putting all entities in generic "entities" cards

### Modern Dashboard Structure

```json
{
  "title": "My Home",
  "icon": "mdi:home",
  "config": {
    "views": [
      {
        "title": "Overview",
        "path": "home",
        "type": "sections",
        "max_columns": 4,
        "sections": [
          {
            "title": "Climate",
            "cards": [...]
          },
          {
            "title": "Lights",
            "cards": [...]
          }
        ]
      },
      {
        "title": "Energy",
        "path": "energy",
        "type": "sections",
        "icon": "mdi:lightning-bolt",
        "sections": [...]
      }
    ]
  }
}
```

**Key Modern Features:**
- **Sections view**: Grid-based layout with named sections
- **max_columns**: Control grid columns (default: 4)
- **path**: URL identifier for view (/lovelace/energy)
- **Multiple views**: Separate concerns (Overview, Energy, Security, etc.)
- **Navigation**: Use navigate actions to link between views

## Critical Validation Rules

### url_path MUST contain hyphen (-)
Dashboard url_path is REJECTED without hyphen. Valid: "my-dashboard", Invalid: "mydashboard"

### Dashboard ID vs url_path
- **dashboard_id**: Internal identifier (returned on create, used for update/delete)
- **url_path**: URL identifier (user-facing, used in dashboard URLs)

## View Types and When to Use Them

### Sections View (RECOMMENDED for Modern Dashboards)
**Use for:** Most dashboards, organized content, responsive layouts

```json
{
  "type": "sections",
  "title": "Living Room",
  "path": "living-room",
  "max_columns": 4,
  "sections": [
    {
      "title": "Climate Control",
      "cards": [
        {"type": "tile", "entity": "climate.living_room", "features": [...]},
        {"type": "tile", "entity": "fan.ceiling"}
      ]
    },
    {
      "title": "Lighting",
      "cards": [
        {"type": "grid", "columns": 3, "square": false, "cards": [
          {"type": "tile", "entity": "light.overhead"},
          {"type": "tile", "entity": "light.lamp"},
          {"type": "tile", "entity": "light.accent"}
        ]}
      ]
    }
  ]
}
```

**Benefits:**
- Grid-based responsive layout
- Named sections for organization
- Precise control over card placement
- Works well on mobile and desktop

### Masonry View (LEGACY)
**Use for:** Quick prototypes only - sections view is preferred

Auto-arranges cards in columns based on card height. Less control, less modern.

### Panel View
**Use for:** Full-screen single cards (maps, cameras, iframes)

```json
{
  "type": "panel",
  "title": "Security Cameras",
  "path": "cameras",
  "cards": [
    {"type": "picture-glance", "camera_image": "camera.front_door", "entities": [...]}
  ]
}
```

### Sidebar View
**Use for:** Two-column layouts with primary/secondary content

Main content on left (wide), secondary on right (narrow sidebar).

## Modern Card Patterns

### Grid Card (Primary Layout Tool)

**Use for:** Organizing multiple cards into columns within a section

```json
{
  "type": "grid",
  "columns": 3,
  "square": false,
  "cards": [
    {"type": "tile", "entity": "light.kitchen"},
    {"type": "tile", "entity": "light.dining"},
    {"type": "tile", "entity": "light.hallway"}
  ]
}
```

**When to use grid vs sections:**
- **Sections**: Top-level organization of dashboard views
- **Grid cards**: Within sections to create multi-column layouts

### Tile Card (Modern Entity Control)

**Primary card type for modern dashboards** - replaces most legacy cards

```json
{
  "type": "tile",
  "entity": "climate.bedroom",
  "name": "Master Bedroom",
  "icon": "mdi:thermostat",
  "features": [
    {"type": "target-temperature"},
    {"type": "climate-hvac-modes", "style": "dropdown"},
    {"type": "climate-fan-modes", "style": "icons"}
  ],
  "tap_action": {"action": "more-info"}
}
```

**Benefits:**
- Clean, modern design
- Integrated quick controls (features)
- Responsive sizing
- Works with all entity domains

### Navigation Between Views

**Modern Pattern:** Create focused views and link between them

```json
{
  "type": "button",
  "name": "View Energy Details",
  "icon": "mdi:chart-line",
  "tap_action": {
    "action": "navigate",
    "navigation_path": "/lovelace/energy"
  }
}
```

**Use cases:**
- Overview view → Detail views
- Subviews for specific areas/systems
- Modal-style detail pages

## Comprehensive View Configuration

### View Structure
```json
{
  "title": "View Name",
  "path": "unique-path",
  "type": "sections",
  "icon": "mdi:icon",
  "theme": "theme-name",
  "badges": ["sensor.entity_id"],
  "max_columns": 4,
  "sections": [...],
  "subview": false,
  "visible": true,
  "background": {
    "image": "url(/local/background.jpg)",
    "opacity": 0.3
  }
}
```

**Key Properties:**
- `path`: URL identifier for deep linking
- `type`: sections, masonry, panel, sidebar (default: sections)
- `max_columns`: Grid columns for sections view (default: 4)
- `badges`: Entity IDs displayed at top
- `visible`: Boolean or user ID list for conditional display
- `subview`: true = hidden from navigation (requires back_path)
- `background`: Image/color background settings

### Sections (Sections View Only)

```json
{
  "sections": [
    {
      "title": "Section Title",
      "cards": [...]
    },
    {
      "title": "Another Section",
      "cards": [...]
    }
  ]
}
```

## Card Categories

**Modern Primary Cards:** tile, area, button, grid
**Container Cards:** vertical-stack, horizontal-stack, grid
**Logic Cards:** conditional, entity-filter
**Display Cards:** sensor, history-graph, statistics-graph, gauge, energy, webpage, calendar, logbook, clock
**Control Cards (Legacy):** entity, entities, light, thermostat, humidifier, alarm-panel
**Hybrid Cards:** picture-elements, picture-glance, glance, heading

**Recommendation:** Use `tile` card for most entities instead of legacy control cards.

## Card Configuration

### Common Card Structure
```json
{
  "type": "tile",
  "entity": "light.living_room",
  "name": "Custom Name",
  "icon": "mdi:lightbulb",
  "color": "blue",
  "features": [
    {"type": "light-brightness"},
    {"type": "light-color-temp"}
  ],
  "tap_action": {"action": "toggle"},
  "hold_action": {"action": "more-info"}
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
  "double_tap_action": {
    "action": "navigate",
    "navigation_path": "/lovelace/lights"
  }
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

## Modern Dashboard Examples

### Multi-View Dashboard with Sections

```json
{
  "title": "Modern Home",
  "icon": "mdi:home",
  "config": {
    "views": [
      {
        "title": "Overview",
        "path": "home",
        "type": "sections",
        "max_columns": 4,
        "badges": ["person.john", "person.jane"],
        "sections": [
          {
            "title": "Quick Actions",
            "cards": [
              {
                "type": "grid",
                "columns": 4,
                "square": false,
                "cards": [
                  {"type": "button", "name": "Lights", "icon": "mdi:lightbulb", "tap_action": {"action": "navigate", "navigation_path": "/lovelace/lights"}},
                  {"type": "button", "name": "Climate", "icon": "mdi:thermostat", "tap_action": {"action": "navigate", "navigation_path": "/lovelace/climate"}},
                  {"type": "button", "name": "Security", "icon": "mdi:shield-home", "tap_action": {"action": "navigate", "navigation_path": "/lovelace/security"}},
                  {"type": "button", "name": "Energy", "icon": "mdi:lightning-bolt", "tap_action": {"action": "navigate", "navigation_path": "/lovelace/energy"}}
                ]
              }
            ]
          },
          {
            "title": "Favorites",
            "cards": [
              {
                "type": "grid",
                "columns": 3,
                "square": false,
                "cards": [
                  {"type": "tile", "entity": "light.living_room", "features": [{"type": "light-brightness"}]},
                  {"type": "tile", "entity": "climate.bedroom", "features": [{"type": "target-temperature"}]},
                  {"type": "tile", "entity": "lock.front_door"}
                ]
              }
            ]
          }
        ]
      },
      {
        "title": "Lights",
        "path": "lights",
        "type": "sections",
        "icon": "mdi:lightbulb",
        "max_columns": 3,
        "sections": [
          {
            "title": "Living Room",
            "cards": [
              {
                "type": "grid",
                "columns": 3,
                "cards": [
                  {"type": "tile", "entity": "light.overhead", "features": [{"type": "light-brightness"}]},
                  {"type": "tile", "entity": "light.lamp", "features": [{"type": "light-brightness"}]},
                  {"type": "tile", "entity": "light.accent", "features": [{"type": "light-color-temp"}]}
                ]
              }
            ]
          },
          {
            "title": "Kitchen",
            "cards": [
              {
                "type": "grid",
                "columns": 2,
                "cards": [
                  {"type": "tile", "entity": "light.kitchen_main"},
                  {"type": "tile", "entity": "light.under_cabinet"}
                ]
              }
            ]
          }
        ]
      }
    ]
  }
}
```

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
Entity domain should be compatible with card type:
- **Recommended:** Use `tile` card for all entity types
- light entities → tile, light card, entity card
- climate entities → tile, thermostat card
- sensor entities → tile, sensor card, gauge card

### Features Compatibility
Features only work on specific cards:
- climate-* features → tile card (climate entity), thermostat card
- light-* features → tile card (light entity), light card
- Check card type + entity domain match

### Metadata Updates
Use ha_config_update_dashboard_metadata() for title/icon changes
Use ha_config_set_dashboard() for config changes
Requires dashboard_id NOT url_path

### View vs Sections Confusion
- **View**: Tab in dashboard (has `title`, `path`, `type`)
- **Sections**: Grouping within a "sections" view type
- Don't use `sections` property on non-sections view types

## Resource References

Card type documentation: `ha-dashboard://card-docs/{card-type}`
Available card types: `ha-dashboard://card-types`

Examples:
- `ha-dashboard://card-docs/tile` → Tile card documentation (RECOMMENDED)
- `ha-dashboard://card-docs/grid` → Grid card documentation
- `ha-dashboard://card-docs/light` → Light card documentation
- `ha-dashboard://card-types` → List of all 41 card types
