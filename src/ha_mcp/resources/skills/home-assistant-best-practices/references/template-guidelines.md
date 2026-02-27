# Template Guidelines

This document covers when templates ARE the right choice in Home Assistant, and best practices for writing reliable templates.

## Table of Contents
1. [When Templates Are Appropriate](#when-templates-are-appropriate)
2. [When to Avoid Templates](#when-to-avoid-templates)
3. [Template Sensor Best Practices](#template-sensor-best-practices)
4. [Automation Template Best Practices](#automation-template-best-practices)
5. [Common Patterns](#common-patterns)
6. [Error Handling](#error-handling)
7. [Performance Considerations](#performance-considerations)

---

## When Templates Are Appropriate

Templates are the RIGHT choice when:

### 1. Dynamic Service Data

You need to pass dynamic values to service calls based on entity states or trigger context.

```yaml
action:
  - action: light.turn_on
    target:
      entity_id: light.bedroom
    data:
      brightness_pct: "{{ states('input_number.default_brightness') | int }}"
      kelvin: "{{ 6500 if is_state('binary_sensor.daytime', 'on') else 2700 }}"
```

### 2. Dynamic Notification Messages

Messages that include runtime information:

```yaml
action:
  - action: notify.mobile_app
    data:
      message: >
        {{ trigger.to_state.name }} has been {{ trigger.to_state.state }} 
        for {{ trigger.for.total_seconds() | int // 60 }} minutes.
```

### 3. Processing Raw Data Sources

MQTT, REST, and command line sensors often provide raw data that needs transformation:

```yaml
# REST sensor with JSON response
rest:
  - resource: "http://api.example.com/data"
    sensor:
      - name: "Temperature"
        value_template: "{{ value_json.current.temperature }}"
        unit_of_measurement: "°C"
```

### 4. Accessing Trigger Context

Using `trigger.` variables in automations:

```yaml
action:
  - action: notify.mobile_app
    data:
      message: >
        {{ trigger.to_state.name }} changed from 
        {{ trigger.from_state.state }} to {{ trigger.to_state.state }}
```

### 5. Complex String Formatting

When you need formatted output that can't be achieved with native constructs:

```yaml
template:
  - sensor:
      - name: "Friendly Uptime"
        state: >
          {% set uptime = states('sensor.system_uptime') | float(0) %}
          {% set days = (uptime // 86400) | int %}
          {% set hours = ((uptime % 86400) // 3600) | int %}
          {% set minutes = ((uptime % 3600) // 60) | int %}
          {{ days }}d {{ hours }}h {{ minutes }}m
```

### 6. Attribute Extraction

Creating sensors from entity attributes:

```yaml
template:
  - sensor:
      - name: "Current Song Artist"
        state: "{{ state_attr('media_player.spotify', 'media_artist') }}"
```

### 7. Complex Conditional State

When the state depends on multiple factors that can't be expressed with native conditions:

```yaml
template:
  - sensor:
      - name: "Comfort Level"
        state: >
          {% set temp = states('sensor.temperature') | float(20) %}
          {% set humidity = states('sensor.humidity') | float(50) %}
          {% if temp >= 20 and temp <= 24 and humidity >= 40 and humidity <= 60 %}
            Comfortable
          {% elif temp < 18 or humidity < 30 %}
            Too Cold/Dry
          {% elif temp > 26 or humidity > 70 %}
            Too Hot/Humid
          {% else %}
            Acceptable
          {% endif %}
```

### 8. Entity Iteration

Processing multiple entities dynamically:

```yaml
template:
  - sensor:
      - name: "Open Windows Count"
        state: >
          {{ states.binary_sensor
             | selectattr('attributes.device_class', 'eq', 'window')
             | selectattr('state', 'eq', 'on')
             | list
             | count }}
```

### 9. Date/Time Calculations

Time differences, formatting, and calculations:

```yaml
template:
  - sensor:
      - name: "Days Until Event"
        state: >
          {% set event_date = as_datetime(states('input_datetime.event')) %}
          {% set today = now().replace(hour=0, minute=0, second=0, microsecond=0) %}
          {{ ((event_date - today).days) }}
        unit_of_measurement: "days"
```

---

## When to Avoid Templates

Do NOT use templates when a native alternative exists:

| Don't Use Template | Use Native |
|-------------------|------------|
| `{{ states('x') in ['a', 'b'] }}` | `condition: state` with `state: ["a", "b"]` |
| `{{ states('x') \| float > 25 }}` | `condition: numeric_state` with `above: 25` |
| `{{ now().hour >= 9 }}` | `condition: time` with `after: "09:00:00"` |
| `{{ is_state('sun.sun', 'below_horizon') }}` | `condition: sun` with `after: sunset` |
| `wait_template: "{{ is_state(...) }}"` | `wait_for_trigger` with state trigger |
| Template sensor summing values | `min_max` helper with `type: sum` |
| Template binary sensor with threshold | `threshold` helper |
| Template sensor averaging over time | `statistics` helper |

See `automation-patterns.md` and `helper-selection.md` for comprehensive alternatives.

---

## Template Sensor Best Practices

### Always Include unique_id

```yaml
template:
  - sensor:
      - name: "My Sensor"
        unique_id: my_custom_sensor  # Enables UI customization
        state: "{{ states('sensor.source') }}"
```

### Always Define Availability

Prevent errors and unknown states:

```yaml
template:
  - sensor:
      - name: "Safe Sensor"
        unique_id: safe_sensor
        availability: >
          {{ has_value('sensor.source_a') and 
             has_value('sensor.source_b') }}
        state: >
          {{ states('sensor.source_a') | float + 
             states('sensor.source_b') | float }}
```

### Use Appropriate Device Class

Helps with unit conversion and graph display:

```yaml
template:
  - sensor:
      - name: "Calculated Temperature"
        device_class: temperature
        unit_of_measurement: "°C"
        state_class: measurement
        state: "{{ states('sensor.raw_temp') | float / 10 }}"
```

### Use state_class for Statistics

Required for long-term statistics:

```yaml
template:
  - sensor:
      - name: "Power Usage"
        device_class: power
        unit_of_measurement: "W"
        state_class: measurement  # Enables statistics
        state: "{{ states('sensor.amps') | float * 230 }}"
```

### Use Trigger-Based for Efficiency

Trigger-based templates only update when sources change:

```yaml
template:
  - trigger:
      - trigger: state
        entity_id: 
          - sensor.temp_bedroom
          - sensor.temp_living
    sensor:
      - name: "Average Temperature"
        state: >
          {% set temps = [
            states('sensor.temp_bedroom') | float(0),
            states('sensor.temp_living') | float(0)
          ] %}
          {{ (temps | sum / temps | count) | round(1) }}
        unit_of_measurement: "°C"
```

**Benefits of trigger-based:**
- Only evaluates when trigger fires (not on every state change)
- Access to `trigger` variable
- More efficient for complex templates

---

## Automation Template Best Practices

### Use Shorthand Syntax

For template conditions, use the shorthand:

```yaml
# Shorthand (preferred)
condition:
  - "{{ trigger.to_state.attributes.brightness > 100 }}"

# Long form (equivalent but verbose)
condition:
  - condition: template
    value_template: "{{ trigger.to_state.attributes.brightness > 100 }}"
```

### Use Multiline Strings

For readability in complex templates:

```yaml
action:
  - action: notify.mobile_app
    data:
      message: >
        {% if is_state('binary_sensor.door', 'on') %}
          Warning: Door is open!
        {% else %}
          All secure.
        {% endif %}
```

### Access Trigger Context Properly

```yaml
automation:
  - trigger:
      - trigger: state
        entity_id: light.bedroom
    action:
      - action: notify.mobile_app
        data:
          message: >
            Light changed from {{ trigger.from_state.state }} 
            to {{ trigger.to_state.state }}
            Entity: {{ trigger.entity_id }}
            Brightness: {{ trigger.to_state.attributes.brightness | default('N/A') }}
```

---

## Common Patterns

### Safe State Access

Always use `states()` function, not `states.sensor.x.state`:

```yaml
# GOOD - Returns 'unknown' if entity doesn't exist
{{ states('sensor.temperature') }}

# BAD - Errors if entity doesn't exist
{{ states.sensor.temperature.state }}
```

### Safe Numeric Conversion

```yaml
# GOOD - Default value if conversion fails
{{ states('sensor.temperature') | float(0) }}

# BAD - Errors if state is 'unavailable' or 'unknown'
{{ states('sensor.temperature') | float }}
```

### Check for Valid State

```yaml
{% if has_value('sensor.temperature') %}
  Temperature is {{ states('sensor.temperature') }}°C
{% else %}
  Temperature unavailable
{% endif %}
```

### Multiple States Check

```yaml
{% if is_state('light.a', 'on') and is_state('light.b', 'on') %}
  Both lights on
{% endif %}
```

### List of States

```yaml
{% if states('alarm_control_panel.home') in ['armed_home', 'armed_away', 'armed_night'] %}
  Alarm is armed
{% endif %}
```

### Attribute Access with Default

```yaml
{{ state_attr('light.bedroom', 'brightness') | default(0) }}
```

### Time Since State Change

```yaml
{% set last_changed = states.binary_sensor.motion.last_changed %}
{% set seconds = (now() - last_changed).total_seconds() %}
{{ (seconds / 60) | round(0) }} minutes ago
```

### Filter Entities by Attribute

```yaml
{% set open_windows = states.binary_sensor
   | selectattr('attributes.device_class', 'defined')
   | selectattr('attributes.device_class', 'eq', 'window')
   | selectattr('state', 'eq', 'on')
   | list %}
{{ open_windows | count }} windows open
```

### Iterate with Index

```yaml
{% for light in states.light %}
  {{ loop.index }}: {{ light.name }} is {{ light.state }}
{% endfor %}
```

### Format Lists Human-Readable

```yaml
{% set items = ['apples', 'oranges', 'bananas'] %}
{{ items[:-1] | join(', ') }} and {{ items[-1] }}
{# Output: apples, oranges and bananas #}
```

---

## Error Handling

### Default Values

```yaml
# For numeric operations
{{ states('sensor.x') | float(default=0) }}
{{ states('sensor.x') | int(default=-1) }}

# For attribute access
{{ state_attr('light.x', 'brightness') | default(100) }}

# For entire template failures
{{ states('sensor.missing') | default('Unknown', true) }}
```

### Availability Template

```yaml
template:
  - sensor:
      - name: "Calculated Value"
        availability: "{{ has_value('sensor.input') }}"
        state: "{{ states('sensor.input') | float * 2 }}"
```

### Check Before Use

```yaml
{% if is_state_attr('media_player.tv', 'is_volume_muted', false) %}
  Volume: {{ state_attr('media_player.tv', 'volume_level') | float * 100 }}%
{% else %}
  Muted
{% endif %}
```

### Handle None Attributes

```yaml
{% set attr = state_attr('sensor.x', 'some_attr') %}
{% if attr is not none %}
  Attribute value: {{ attr }}
{% else %}
  Attribute not available
{% endif %}
```

---

## Performance Considerations

### Avoid Expensive Operations in Value Templates

Templates in `value_template` for sensors update on EVERY state change of the source entity.

```yaml
# EXPENSIVE - Runs on every source sensor update
sensor:
  - platform: template
    sensors:
      expensive_sensor:
        value_template: >
          {% for entity in states %}  {# Iterates ALL entities #}
            ...
          {% endfor %}
```

### Use Trigger-Based Templates for Complex Logic

```yaml
# EFFICIENT - Only runs when specified triggers fire
template:
  - trigger:
      - trigger: time_pattern
        minutes: "/5"  # Every 5 minutes
    sensor:
      - name: "Complex Calculation"
        state: >
          {% set total = 0 %}
          {% for sensor in states.sensor | selectattr('attributes.device_class', 'eq', 'energy') %}
            {% set total = total + states(sensor.entity_id) | float(0) %}
          {% endfor %}
          {{ total }}
```

### Cache Complex Calculations

If you need the same value in multiple places, create one template sensor and reference it:

```yaml
# ONE template sensor
template:
  - sensor:
      - name: "House Occupancy Count"
        state: >
          {{ states.person | selectattr('state', 'eq', 'home') | list | count }}

# Reference it elsewhere
automation:
  - condition: numeric_state
    entity_id: sensor.house_occupancy_count
    above: 0
```

### Use Variables for Repeated Access

```yaml
{% set temp = states('sensor.temperature') | float(0) %}
{% set humidity = states('sensor.humidity') | float(0) %}

{% if temp > 25 and humidity > 70 %}
  Hot and humid
{% elif temp > 25 %}
  Hot (temp: {{ temp }}°C)
{% endif %}
```

---

## Quick Reference: Functions and Filters

### State Functions

| Function | Purpose |
|----------|---------|
| `states('entity_id')` | Get entity state (string) |
| `state_attr('entity_id', 'attr')` | Get attribute value |
| `is_state('entity_id', 'state')` | Check if entity has state |
| `is_state_attr('entity_id', 'attr', 'value')` | Check attribute value |
| `has_value('entity_id')` | True if not unknown/unavailable |

### Common Filters

| Filter | Purpose |
|--------|---------|
| `float(default)` | Convert to float |
| `int(default)` | Convert to int |
| `round(precision)` | Round number |
| `default(value)` | Provide fallback |
| `timestamp_custom(format)` | Format timestamp |
| `from_json` | Parse JSON string |
| `to_json` | Convert to JSON string |
| `regex_match(pattern)` | Regex match |
| `regex_replace(find, replace)` | Regex replace |

### Time Functions

| Function | Purpose |
|----------|---------|
| `now()` | Current datetime |
| `utcnow()` | Current UTC datetime |
| `today_at('HH:MM')` | Today at specific time |
| `as_timestamp(dt)` | Convert to Unix timestamp |
| `as_datetime(ts)` | Convert from timestamp |
| `as_timedelta(string)` | Parse duration string |

### Collection Filters

| Filter | Purpose |
|--------|---------|
| `selectattr('attr', 'eq', 'value')` | Filter by attribute |
| `rejectattr('attr', 'eq', 'value')` | Exclude by attribute |
| `map(attribute='state')` | Extract attribute from list |
| `list` | Convert to list |
| `count` | Count items |
| `first` / `last` | Get first/last item |
| `sum` / `min` / `max` | Aggregate values |
