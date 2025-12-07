# Home Assistant Dashboard Guide

Complete guide for designing Home Assistant dashboards: structure, built-in cards, custom cards, CSS styling, and HACS integration.

---

## Part 1: Dashboard Structure

### Modern Dashboard Best Practices

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

### Dashboard Structure

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
          {"title": "Climate", "cards": [...]},
          {"title": "Lights", "cards": [...]}
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

### Critical Validation Rules

**url_path MUST contain hyphen (-)**
- Valid: "my-dashboard"
- Invalid: "mydashboard" → REJECTED

**Dashboard ID vs url_path:**
- `dashboard_id`: Internal identifier (returned on create, used for update/delete)
- `url_path`: URL identifier (user-facing, used in dashboard URLs)

### View Types

| Type | Use For |
|------|---------|
| `sections` | Most dashboards (RECOMMENDED) - grid-based, responsive |
| `panel` | Full-screen single cards (maps, cameras, iframes) |
| `sidebar` | Two-column layouts with primary/secondary content |
| `masonry` | Legacy - auto-arranges cards, less control |

### View Configuration

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
  "background": {"image": "url(/local/background.jpg)", "opacity": 0.3}
}
```

---

## Part 2: Built-in Cards

### Card Categories

| Category | Cards |
|----------|-------|
| **Modern Primary** | tile, area, button, grid |
| **Container** | vertical-stack, horizontal-stack, grid |
| **Logic** | conditional, entity-filter |
| **Display** | sensor, history-graph, statistics-graph, gauge, energy, calendar |
| **Legacy Control** | entity, entities, light, thermostat (use tile instead) |

**Recommendation:** Use `tile` card for most entities.

### Tile Card (Modern Entity Control)

```json
{
  "type": "tile",
  "entity": "climate.bedroom",
  "name": "Master Bedroom",
  "icon": "mdi:thermostat",
  "features": [
    {"type": "target-temperature"},
    {"type": "climate-hvac-modes", "style": "dropdown"}
  ],
  "tap_action": {"action": "more-info"}
}
```

### Grid Card (Layout Tool)

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

### Features (Quick Controls)

Available on: tile, area, humidifier, thermostat cards

**Climate:** climate-hvac-modes, climate-fan-modes, climate-preset-modes, target-temperature
**Light:** light-brightness, light-color-temp
**Cover:** cover-open-close, cover-position, cover-tilt
**Fan:** fan-speed, fan-direction, fan-oscillate
**Media:** media-player-playback, media-player-volume-slider
**Other:** toggle, button, alarm-modes, lock-commands, numeric-input

Feature `style` options: "dropdown" or "icons"

### Actions

```json
{
  "tap_action": {"action": "toggle"},
  "hold_action": {"action": "more-info"},
  "double_tap_action": {"action": "navigate", "navigation_path": "/lovelace/lights"}
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

---

## Part 3: Custom Cards (JavaScript Modules)

For functionality beyond built-in cards, create custom cards using JavaScript modules.

### When to Use Custom Cards

- Built-in cards don't support your visualization
- Need complex interactive behavior
- Want to integrate external libraries
- Creating reusable components

### Minimal Custom Card

```javascript
class MySimpleCard extends HTMLElement {
  setConfig(config) {
    if (!config.entity) {
      throw new Error("Please define an entity");
    }
    this.config = config;
  }

  set hass(hass) {
    if (!this.content) {
      this.innerHTML = `
        <ha-card header="${this.config.title || 'My Card'}">
          <div class="card-content"></div>
        </ha-card>
      `;
      this.content = this.querySelector(".card-content");
    }

    const state = hass.states[this.config.entity];
    this.content.innerHTML = state
      ? `State: ${state.state}`
      : "Entity not found";
  }

  getCardSize() {
    return 2;
  }
}

customElements.define("my-simple-card", MySimpleCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "my-simple-card",
  name: "My Simple Card",
  description: "A simple custom card"
});
```

### Custom Card with Shadow DOM Styling

```javascript
class StyledCard extends HTMLElement {
  setConfig(config) {
    this.config = config;
  }

  set hass(hass) {
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
      this.shadowRoot.innerHTML = `
        <style>
          :host { display: block; }
          .card {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border-radius: 16px;
            padding: 20px;
            color: white;
          }
          .value { font-size: 2em; font-weight: bold; }
        </style>
        <div class="card">
          <div class="label"></div>
          <div class="value"></div>
        </div>
      `;
    }

    const state = hass.states[this.config.entity];
    if (state) {
      this.shadowRoot.querySelector(".label").textContent =
        state.attributes.friendly_name || this.config.entity;
      this.shadowRoot.querySelector(".value").textContent =
        `${state.state} ${state.attributes.unit_of_measurement || ""}`;
    }
  }

  getCardSize() {
    return 2;
  }
}

customElements.define("styled-card", StyledCard);
```

### Using Custom Cards

```yaml
type: custom:my-simple-card
entity: sensor.temperature
title: Temperature
```

### Hosting Custom Cards

Use `ha_create_dashboard_resource` to convert inline code to a hosted URL:

```python
result = ha_create_dashboard_resource(
    content=card_code,
    resource_type="module"
)
# Returns: {"url": "https://...", "size": ...}
```

Then register the URL as a dashboard resource.

**Size limits:** ~24KB source code (32KB encoded URL)

---

## Part 4: CSS Styling

### CSS Resource for Theme Overrides

```css
:root {
  --primary-color: #03a9f4;
  --accent-color: #ff5722;
  --ha-card-background: rgba(26, 26, 46, 0.9);
  --ha-card-border-radius: 16px;
  --ha-card-box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
}
```

### Card-Specific Styling

```css
/* Style all entity cards */
hui-entities-card ha-card {
  background: linear-gradient(180deg, #1a1a2e 0%, #16213e 100%);
}

/* Style specific card by data attribute */
ha-card[data-card="weather"] {
  border: 2px solid var(--primary-color);
}
```

### Using CSS Resources

```python
result = ha_create_dashboard_resource(
    content=css_code,
    resource_type="css"
)
```

### Card-mod Integration

For per-card styling, use the card-mod custom component from HACS:

```yaml
type: entities
card_mod:
  style: |
    ha-card {
      --ha-card-background: teal;
      color: var(--primary-color);
    }
entities:
  - light.bed_light
```

---

## Part 5: HACS Integration

HACS (Home Assistant Community Store) provides 700+ custom cards.

### When to Use HACS vs Inline Resources

| Use Case | Solution |
|----------|----------|
| Popular community card | HACS - `ha_hacs_search` + `ha_hacs_download` |
| Small custom styling | Inline CSS - `ha_create_dashboard_resource` |
| One-off custom card | Inline module - `ha_create_dashboard_resource` |
| Large/complex card | HACS or filesystem (`/config/www/`) |

### Finding Cards

```python
# Search for cards
result = ha_hacs_search(query="mushroom", category="lovelace")

# Get repository details
result = ha_hacs_repository_info(repository_id="piitaya/lovelace-mushroom")
```

### Installing Cards

```python
# Install from HACS (requires user confirmation)
result = ha_hacs_download(repository_id="piitaya/lovelace-mushroom")
```

**Note:** HACS install operations have `destructiveHint: True` - clients will ask for user confirmation.

### Popular HACS Cards

- **mushroom** - Modern, clean card collection
- **button-card** - Highly customizable buttons
- **mini-graph-card** - Compact graphs
- **card-mod** - CSS styling for any card
- **layout-card** - Advanced layout control
- **apexcharts-card** - Professional charts

---

## Part 6: Complete Examples

### Multi-View Dashboard

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
            "cards": [{
              "type": "grid",
              "columns": 4,
              "square": false,
              "cards": [
                {"type": "button", "name": "Lights", "icon": "mdi:lightbulb", "tap_action": {"action": "navigate", "navigation_path": "/lovelace/lights"}},
                {"type": "button", "name": "Climate", "icon": "mdi:thermostat", "tap_action": {"action": "navigate", "navigation_path": "/lovelace/climate"}},
                {"type": "button", "name": "Security", "icon": "mdi:shield-home", "tap_action": {"action": "navigate", "navigation_path": "/lovelace/security"}},
                {"type": "button", "name": "Energy", "icon": "mdi:lightning-bolt", "tap_action": {"action": "navigate", "navigation_path": "/lovelace/energy"}}
              ]
            }]
          },
          {
            "title": "Favorites",
            "cards": [{
              "type": "grid",
              "columns": 3,
              "square": false,
              "cards": [
                {"type": "tile", "entity": "light.living_room", "features": [{"type": "light-brightness"}]},
                {"type": "tile", "entity": "climate.bedroom", "features": [{"type": "target-temperature"}]},
                {"type": "tile", "entity": "lock.front_door"}
              ]
            }]
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
            "cards": [{
              "type": "grid",
              "columns": 3,
              "cards": [
                {"type": "tile", "entity": "light.overhead", "features": [{"type": "light-brightness"}]},
                {"type": "tile", "entity": "light.lamp", "features": [{"type": "light-brightness"}]},
                {"type": "tile", "entity": "light.accent", "features": [{"type": "light-color-temp"}]}
              ]
            }]
          }
        ]
      }
    ]
  }
}
```

### Custom Card Workflow

```python
# 1. Create the card code
card_code = '''
class QuickStatusCard extends HTMLElement {
  setConfig(config) { this.config = config; }
  set hass(hass) {
    const state = hass.states[this.config.entity];
    this.innerHTML = `<ha-card>
      <div style="padding:16px;text-align:center;">
        <div style="font-size:2em;">${state?.state || "?"}</div>
        <div>${this.config.name || this.config.entity}</div>
      </div>
    </ha-card>`;
  }
  getCardSize() { return 2; }
}
customElements.define("quick-status-card", QuickStatusCard);
'''

# 2. Get hosted URL
result = ha_create_dashboard_resource(content=card_code, resource_type="module")

# 3. Register as dashboard resource

# 4. Use in dashboard
card_config = {
  "type": "custom:quick-status-card",
  "entity": "sensor.temperature",
  "name": "Living Room"
}
```

---

## Common Pitfalls

| Issue | Solution |
|-------|----------|
| url_path rejected | Add hyphen: "my-dashboard" not "mydashboard" |
| Entity not found | Use full ID: "light.living_room" not "living_room" |
| Features not working | Match feature type to entity domain |
| Custom card not loading | Check resource type is "module", verify URL |
| Card too large for inline | Use HACS or filesystem instead |

---

## Part 7: Advanced Workflow - Visual Iteration

For iterative dashboard design with visual feedback, add a browser automation MCP server:

### Recommended MCP Servers

- **Playwright MCP** (`@anthropic/mcp-playwright`) - Take screenshots, interact with pages
- **Puppeteer MCP** - Similar browser automation capabilities
- **Browser DevTools MCP** - Inspect elements, debug layouts

### Visual Iteration Workflow

```
1. Create/update dashboard with ha_update_dashboard_view()
2. Navigate browser to dashboard URL (e.g., http://homeassistant.local:8123/lovelace/my-dashboard)
3. Take screenshot to see current layout
4. Analyze screenshot for issues (spacing, alignment, colors)
5. Adjust configuration and repeat
```

### Example with Playwright MCP

```python
# Get base URL from system overview
overview = ha_get_overview(detail_level="minimal")
base_url = overview["system_info"]["base_url"]  # e.g., "http://homeassistant.local:8123"

# After updating dashboard
ha_update_dashboard_view(dashboard_id="...", view_index=0, config={...})

# Use Playwright MCP to capture result
browser_navigate(f"{base_url}/lovelace/my-dashboard")
browser_screenshot()  # Returns image for visual analysis

# Analyze and iterate based on what you see
```

### Benefits

- See actual rendered output, not just JSON config
- Catch visual issues (card overlap, responsive breakpoints)
- Verify custom card styling
- Test on different viewport sizes

---

## Related Tools

- `ha_get_dashboard_guide` - This guide
- `ha_get_overview` - Get system info including base_url for browser navigation
- `ha_config_set_inline_dashboard_resource` - Host inline JS/CSS
- `ha_config_set_dashboard_resource` - Register external resources
- `ha_update_dashboard_view` - Update dashboard views
- `ha_hacs_search` - Find HACS cards
- `ha_hacs_download` - Install HACS cards
