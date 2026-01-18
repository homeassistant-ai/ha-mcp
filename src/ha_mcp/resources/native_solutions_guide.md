# Native Solutions First: Avoiding Template Overuse

When configuring Home Assistant, **always prefer native solutions over Jinja2 templates**. Templates should be a last resort, not the default approach.

---

## Why Native Solutions Matter

| Native Solutions | Jinja2 Templates |
|------------------|------------------|
| Validated at save time | Errors appear only at runtime |
| Self-documenting UI | Requires reading template code |
| Better performance | Evaluated on every state change |
| Easier to debug | Silent failures, complex debugging |
| Maintained by HA team | User responsibility |

**Rule of thumb:** If Home Assistant provides a native way to accomplish something, use it. Templates add complexity and failure modes.

---

## Part 1: Automations

### Conditions: Native vs Template

**PREFER native conditions** - Home Assistant supports many condition types natively:

#### State Conditions (Multiple Values)

```yaml
# GOOD: Native state condition with list
condition:
  - condition: state
    entity_id: climate.living_room
    state:
      - "heat"
      - "cool"
      - "auto"

# BAD: Template for the same logic
condition:
  - condition: template
    value_template: "{{ states('climate.living_room') in ['heat', 'cool', 'auto'] }}"
```

#### Attribute Conditions

```yaml
# GOOD: Native state condition with attribute
condition:
  - condition: state
    entity_id: climate.bedroom
    attribute: hvac_action
    state: "heating"

# BAD: Template for attribute check
condition:
  - condition: template
    value_template: "{{ state_attr('climate.bedroom', 'hvac_action') == 'heating' }}"
```

#### Numeric Conditions

```yaml
# GOOD: Native numeric_state condition
condition:
  - condition: numeric_state
    entity_id: sensor.temperature
    above: 20
    below: 30

# BAD: Template for numeric comparison
condition:
  - condition: template
    value_template: "{{ states('sensor.temperature') | float > 20 and states('sensor.temperature') | float < 30 }}"
```

#### Time Conditions

```yaml
# GOOD: Native time condition
condition:
  - condition: time
    after: "08:00:00"
    before: "22:00:00"
    weekday:
      - mon
      - tue
      - wed
      - thu
      - fri

# BAD: Template for time check
condition:
  - condition: template
    value_template: "{{ now().hour >= 8 and now().hour < 22 and now().weekday() < 5 }}"
```

#### Compound Conditions (AND/OR/NOT)

```yaml
# GOOD: Native compound conditions
condition:
  - condition: and
    conditions:
      - condition: state
        entity_id: input_boolean.vacation_mode
        state: "off"
      - condition: or
        conditions:
          - condition: state
            entity_id: person.john
            state: "home"
          - condition: state
            entity_id: person.jane
            state: "home"

# BAD: Template for compound logic
condition:
  - condition: template
    value_template: >
      {{ is_state('input_boolean.vacation_mode', 'off') and
         (is_state('person.john', 'home') or is_state('person.jane', 'home')) }}
```

### Triggers: Native vs Template

**PREFER native triggers** - Most trigger scenarios have native support:

#### State Triggers with Attributes

```yaml
# GOOD: Native state trigger with attribute
trigger:
  - platform: state
    entity_id: climate.bedroom
    attribute: current_temperature

# BAD: Template trigger for attribute changes
trigger:
  - platform: template
    value_template: "{{ state_attr('climate.bedroom', 'current_temperature') }}"
```

#### Numeric State Triggers

```yaml
# GOOD: Native numeric_state trigger
trigger:
  - platform: numeric_state
    entity_id: sensor.cpu_temperature
    above: 80
    for:
      minutes: 5

# BAD: Template trigger for numeric threshold
trigger:
  - platform: template
    value_template: "{{ states('sensor.cpu_temperature') | float > 80 }}"
```

#### Multiple Entity Triggers

```yaml
# GOOD: Native trigger with multiple entities
trigger:
  - platform: state
    entity_id:
      - binary_sensor.door_1
      - binary_sensor.door_2
      - binary_sensor.door_3
    to: "on"

# BAD: Template trigger for multiple entities
trigger:
  - platform: template
    value_template: >
      {{ is_state('binary_sensor.door_1', 'on') or
         is_state('binary_sensor.door_2', 'on') or
         is_state('binary_sensor.door_3', 'on') }}
```

### Actions: Native vs Template

#### Wait for Trigger (Not Template)

```yaml
# GOOD: Native wait_for_trigger
action:
  - wait_for_trigger:
      - platform: state
        entity_id: binary_sensor.motion
        to: "off"
        for:
          minutes: 5
    timeout:
      minutes: 30

# BAD: wait_template for the same logic
action:
  - wait_template: "{{ is_state('binary_sensor.motion', 'off') }}"
    timeout:
      minutes: 30
```

#### Service Data from Trigger

```yaml
# GOOD: Use trigger variables directly
action:
  - service: notify.mobile_app
    data:
      message: "{{ trigger.to_state.state }}"
      title: "State changed to"

# Avoid: Unnecessary template wrapping
action:
  - service: notify.mobile_app
    data:
      message: "{{ states(trigger.entity_id) }}"  # Already have trigger.to_state!
```

### When Templates ARE Appropriate in Automations

Use templates when native conditions cannot express the logic:

- Complex string manipulation or formatting
- Mathematical calculations across multiple sensors
- Dynamic entity selection based on runtime state
- Custom date/time calculations beyond native time conditions

---

## Part 2: Helpers

### Built-in Helpers vs Template Sensors

**ALWAYS check if a built-in helper exists** before creating template sensors:

#### Combining Sensor Values

```yaml
# GOOD: Use min_max helper for combining values
- platform: min_max
  name: "Average House Temperature"
  type: mean
  entity_ids:
    - sensor.bedroom_temperature
    - sensor.living_room_temperature
    - sensor.kitchen_temperature

# BAD: Template sensor for averaging
- platform: template
  sensors:
    average_temperature:
      value_template: >
        {{ ((states('sensor.bedroom_temperature') | float) +
            (states('sensor.living_room_temperature') | float) +
            (states('sensor.kitchen_temperature') | float)) / 3 }}
```

#### Summing Power Consumption

```yaml
# GOOD: Use min_max helper with type: sum
- platform: min_max
  name: "Total Power Usage"
  type: sum
  entity_ids:
    - sensor.plug_1_power
    - sensor.plug_2_power
    - sensor.plug_3_power

# BAD: Template sensor for summing
- platform: template
  sensors:
    total_power:
      value_template: >
        {{ (states('sensor.plug_1_power') | float) +
           (states('sensor.plug_2_power') | float) +
           (states('sensor.plug_3_power') | float) }}
```

#### Grouping Entities

```yaml
# GOOD: Use group helper for combined state
group:
  all_doors:
    name: "All Doors"
    entities:
      - binary_sensor.front_door
      - binary_sensor.back_door
      - binary_sensor.garage_door

# Then use: is_state('group.all_doors', 'on')  # Any door open

# BAD: Template binary sensor
- platform: template
  sensors:
    any_door_open:
      value_template: >
        {{ is_state('binary_sensor.front_door', 'on') or
           is_state('binary_sensor.back_door', 'on') or
           is_state('binary_sensor.garage_door', 'on') }}
```

### Helper Type Selection Guide

| Need | Use This Helper | NOT Template |
|------|-----------------|--------------|
| Average/Min/Max/Sum of sensors | `min_max` | Template sensor |
| Any/All of binary sensors | `group` | Template binary sensor |
| Store a number | `input_number` | Template with variable |
| Store a selection | `input_select` | Template with conditionals |
| Store text | `input_text` | Template sensor |
| Store date/time | `input_datetime` | Template with now() |
| Count occurrences | `counter` | Template with math |
| Track time durations | `timer` | Template with timestamps |
| Weekly schedules | `schedule` | Template with weekday checks |
| Presence zones | `zone` | Template with GPS math |

### When Template Sensors ARE Appropriate

Use template sensors when:

- Transforming data (unit conversion, string formatting)
- Calculating derived values not covered by helpers
- Combining data from different domains (weather + sensors)
- Creating computed availability sensors

---

## Part 3: Scripts

### Native Actions vs Template Actions

#### Conditional Execution

```yaml
# GOOD: Native choose action
sequence:
  - choose:
      - conditions:
          - condition: state
            entity_id: input_boolean.guest_mode
            state: "on"
        sequence:
          - service: light.turn_on
            target:
              entity_id: light.guest_room
      - conditions:
          - condition: state
            entity_id: input_boolean.guest_mode
            state: "off"
        sequence:
          - service: light.turn_off
            target:
              entity_id: light.guest_room

# BAD: Template-based conditional service
sequence:
  - service: "light.turn_{{ 'on' if is_state('input_boolean.guest_mode', 'on') else 'off' }}"
    target:
      entity_id: light.guest_room
```

#### Looping Over Entities

```yaml
# GOOD: Native repeat action
sequence:
  - repeat:
      for_each:
        - light.bedroom
        - light.bathroom
        - light.kitchen
      sequence:
        - service: light.turn_on
          target:
            entity_id: "{{ repeat.item }}"
          data:
            brightness_pct: 50

# BAD: Template to generate multiple service calls
# (Not directly possible - would need separate calls)
```

#### Dynamic Delays

```yaml
# GOOD: Native delay with input_number
sequence:
  - delay:
      seconds: "{{ states('input_number.delay_seconds') | int }}"

# This is an appropriate template use - dynamic value injection
# No native alternative exists for variable delays
```

---

## Part 4: Quick Decision Guide

### Before Using a Template, Ask:

1. **Is there a native condition type?**
   - state, numeric_state, time, sun, zone, device, trigger

2. **Is there a built-in helper?**
   - input_*, counter, timer, schedule, min_max, group

3. **Is there a native action?**
   - choose, if/then, repeat, wait_for_trigger, parallel

4. **Does HA support this natively in the trigger/condition field?**
   - Multiple entities, attributes, for duration, match options

### Template Checklist

Only use templates when ALL are true:

- [ ] No native condition/trigger/action accomplishes this
- [ ] No built-in helper provides this functionality
- [ ] The logic genuinely requires dynamic evaluation
- [ ] You've verified the native options in HA documentation

### Common Mistakes to Avoid

| Mistake | Better Approach |
|---------|-----------------|
| Template for state comparison | `condition: state` with state list |
| Template for attribute check | `condition: state` with attribute |
| Template for numeric threshold | `condition: numeric_state` |
| Template sensor for averaging | `min_max` helper with type: mean |
| Template sensor for summing | `min_max` helper with type: sum |
| Template binary for group state | `group` helper |
| wait_template for state | `wait_for_trigger` with state trigger |
| Template service name | `choose` action with conditions |

---

## Further Reading

- `ha_get_domain_docs("automation")` - Full automation documentation
- `ha_get_domain_docs("script")` - Script action reference
- `ha_get_domain_docs("input_number")` - Input helpers
- `ha_get_domain_docs("group")` - Group configuration
- `ha_get_domain_docs("template")` - When templates ARE needed

**Remember:** Templates are powerful but should be your last resort, not your first instinct.
