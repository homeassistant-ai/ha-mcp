# Dashboard Resources Guide

This guide explains how to create and use custom JavaScript modules and CSS for Home Assistant dashboards.

## Resource Types

| Type | Purpose | Use Case |
|------|---------|----------|
| `module` | ES6 JavaScript module | Custom cards, utilities |
| `css` | Stylesheet | Themes, card styling |

**Note:** Always use `module` for JavaScript. The legacy `js` type is for older non-module scripts.

## Creating Inline Resources

Use `ha_create_dashboard_resource` to convert inline code to a hosted URL:

```python
# Create a CSS resource
result = ha_create_dashboard_resource(
    content=".my-card { background: #1a1a2e; }",
    resource_type="css"
)
# Returns: {"url": "https://...", "size": 35}
```

Then register the URL as a dashboard resource.

## Custom Card Structure

### Minimal Custom Card

```javascript
class MySimpleCard extends HTMLElement {
  // Called when config changes
  setConfig(config) {
    if (!config.entity) {
      throw new Error("Please define an entity");
    }
    this.config = config;
  }

  // Called when Home Assistant state changes
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

  // Card height in units (1 unit = 50px)
  getCardSize() {
    return 2;
  }
}

// Register the card
customElements.define("my-simple-card", MySimpleCard);

// Make it discoverable in the UI
window.customCards = window.customCards || [];
window.customCards.push({
  type: "my-simple-card",
  name: "My Simple Card",
  description: "A simple custom card"
});
```

### Using the Card

```yaml
type: custom:my-simple-card
entity: sensor.temperature
title: Temperature
```

## Custom Card with Styling

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
          :host {
            display: block;
          }
          .card {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border-radius: 16px;
            padding: 20px;
            color: white;
          }
          .value {
            font-size: 2em;
            font-weight: bold;
          }
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

## CSS-Only Resources

For styling existing cards, use `type: css`:

### Theme Variables Override

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

## Utility Modules

Create reusable utilities:

```javascript
// Temperature formatting utility
export function formatTemperature(value, unit = "C") {
  const temp = parseFloat(value);
  if (isNaN(temp)) return "N/A";

  if (unit === "F") {
    return `${((temp * 9/5) + 32).toFixed(1)}°F`;
  }
  return `${temp.toFixed(1)}°C`;
}

// State color helper
export function getStateColor(state) {
  const colors = {
    on: "#4caf50",
    off: "#9e9e9e",
    unavailable: "#f44336",
    unknown: "#ff9800"
  };
  return colors[state] || "#2196f3";
}

// Time ago formatter
export function timeAgo(timestamp) {
  const seconds = Math.floor((Date.now() - new Date(timestamp)) / 1000);

  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}
```

## Workflow: Creating a Custom Card

1. **Write the card code** (JavaScript module)
2. **Create hosted URL**: `ha_create_dashboard_resource(content, resource_type="module")`
3. **Register resource**: Add URL to dashboard resources via `ha_call_service` or UI
4. **Use in dashboard**: Add card with `type: custom:your-card-name`

### Example Workflow

```python
# Step 1: Create the card
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

# Step 2: Get hosted URL
result = ha_create_dashboard_resource(content=card_code, resource_type="module")
# result["url"] = "https://ha-mcp-resources.../..."

# Step 3: Register as dashboard resource (via HA service or UI)

# Step 4: Use in dashboard view
card_config = {
  "type": "custom:quick-status-card",
  "entity": "sensor.temperature",
  "name": "Living Room"
}
```

## Size Limits

| Limit | Value |
|-------|-------|
| Max source code | ~24 KB |
| Max encoded URL | ~32 KB |

For larger files, use filesystem access to `/config/www/` instead.

## Best Practices

1. **Use Shadow DOM** for style isolation in complex cards
2. **Handle missing entities** gracefully with fallback states
3. **Keep cards small** - split complex logic into utility modules
4. **Register with window.customCards** for UI discoverability
5. **Test with different themes** to ensure CSS variable compatibility

## Related Tools

- `ha_create_dashboard_resource` - Create hosted URL from inline code
- `ha_update_dashboard_view` - Update dashboard view configuration
- `ha_call_service` - Call HA services (e.g., to reload resources)
- `ha_get_dashboard_views` - Get current dashboard configuration

## Resources

- [Custom Card Documentation](https://developers.home-assistant.io/docs/frontend/custom-ui/custom-card/)
- [Card-mod for CSS Styling](https://github.com/thomasloven/lovelace-card-mod)
- [HACS Custom Cards](https://hacs.xyz/)
