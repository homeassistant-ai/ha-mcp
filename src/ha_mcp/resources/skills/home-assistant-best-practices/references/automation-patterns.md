# Automation Patterns

This document covers native Home Assistant automation constructs that should be used instead of templates.

## Table of Contents
1. [Native Conditions](#native-conditions)
2. [Trigger Types](#trigger-types)
3. [Wait Actions](#wait-actions)
4. [Automation Modes](#automation-modes)
5. [if/then vs choose](#ifthen-vs-choose)
6. [Trigger IDs](#trigger-ids)

---

## Native Conditions

### State Condition

The most common condition type. Supports multiple states, attributes, and duration.

```yaml
# Single state
condition: state
entity_id: light.living_room
state: "on"

# Multiple acceptable states (OR logic)
condition: state
entity_id: vacuum.robot
state:
  - "cleaning"
  - "returning"

# Attribute check
condition: state
entity_id: climate.thermostat
attribute: hvac_action
state: "heating"

# Duration check (entity has been in state for X time)
condition: state
entity_id: binary_sensor.motion
state: "off"
for:
  minutes: 5
```

### Numeric State Condition

For numeric comparisons. Always prefer over template conditions with `| float`.

```yaml
# Above threshold
condition: numeric_state
entity_id: sensor.temperature
above: 25

# Below threshold
condition: numeric_state
entity_id: sensor.humidity
below: 30

# Range
condition: numeric_state
entity_id: sensor.battery
above: 20
below: 80

# Attribute numeric check
condition: numeric_state
entity_id: sun.sun
attribute: elevation
below: -6
```

### Time Condition

For time-based restrictions. Handles midnight crossing automatically.

```yaml
# Time range (handles midnight crossing!)
condition: time
after: "22:00:00"
before: "06:00:00"

# Weekday filter
condition: time
weekday:
  - mon
  - tue
  - wed
  - thu
  - fri

# Combined
condition: time
after: "09:00:00"
before: "17:00:00"
weekday:
  - mon
  - tue
  - wed
  - thu
  - fri
```

### Sun Condition

For sunrise/sunset-based logic with optional offsets.

```yaml
# After sunset
condition: sun
after: sunset

# Before sunrise with offset
condition: sun
before: sunrise
before_offset: "01:00:00"

# After sunset by 30 minutes
condition: sun
after: sunset
after_offset: "00:30:00"
```

### Zone Condition

For presence detection based on zones.

```yaml
condition: zone
entity_id: person.john
zone: zone.home

# Or check if NOT in zone
condition: not
conditions:
  - condition: zone
    entity_id: person.john
    zone: zone.home
```

### And/Or/Not Conditions

Combine conditions with logical operators.

```yaml
# AND (default when multiple conditions listed)
condition: and
conditions:
  - condition: state
    entity_id: light.kitchen
    state: "on"
  - condition: numeric_state
    entity_id: sensor.brightness
    below: 100

# OR
condition: or
conditions:
  - condition: state
    entity_id: person.john
    state: "home"
  - condition: state
    entity_id: person.jane
    state: "home"

# NOT
condition: not
conditions:
  - condition: state
    entity_id: alarm_control_panel.home
    state: "armed_away"

# Shorthand syntax
conditions:
  - and:
      - condition: state
        entity_id: input_boolean.guest_mode
        state: "on"
      - condition: time
        after: "08:00:00"
```

### Template Condition Shorthand

When you must use a template, use the shorthand syntax:

```yaml
# Shorthand (preferred when template is necessary)
conditions:
  - "{{ trigger.to_state.attributes.brightness > 100 }}"
  
# Long form (equivalent)
conditions:
  - condition: template
    value_template: "{{ trigger.to_state.attributes.brightness > 100 }}"
```

---

## Trigger Types

### State Trigger

The workhorse trigger. Fires on entity state changes.

```yaml
# Basic state change to specific value
trigger:
  - trigger: state
    entity_id: binary_sensor.motion
    to: "on"

# From specific state
trigger:
  - trigger: state
    entity_id: light.bedroom
    from: "off"
    to: "on"

# Any state change (omit to/from)
trigger:
  - trigger: state
    entity_id: sensor.temperature

# Attribute change
trigger:
  - trigger: state
    entity_id: climate.thermostat
    attribute: current_temperature

# Duration trigger (entity has been in state for X)
trigger:
  - trigger: state
    entity_id: light.porch
    to: "on"
    for:
      minutes: 30

# Multiple entities
trigger:
  - trigger: state
    entity_id:
      - binary_sensor.motion_kitchen
      - binary_sensor.motion_hallway
    to: "on"
```

### Numeric State Trigger

Fires when crossing a threshold.

```yaml
trigger:
  - trigger: numeric_state
    entity_id: sensor.temperature
    above: 25
    for:
      minutes: 5
```

### Time Trigger

Fires at specific times.

```yaml
# Fixed time
trigger:
  - trigger: time
    at: "07:00:00"

# Input datetime helper
trigger:
  - trigger: time
    at: input_datetime.morning_alarm

# Time pattern (every 5 minutes)
trigger:
  - trigger: time_pattern
    minutes: "/5"

# Every hour at :30
trigger:
  - trigger: time_pattern
    minutes: 30
```

### Sun Trigger

Fires at sunrise/sunset with optional offset.

```yaml
trigger:
  - trigger: sun
    event: sunset
    offset: "-00:30:00"  # 30 minutes before sunset
```

### Event Trigger

Fires on Home Assistant events.

```yaml
# ZHA button event
trigger:
  - trigger: event
    event_type: zha_event
    event_data:
      device_ieee: "00:11:22:33:44:55:66:77"
      command: "on"

# Custom event
trigger:
  - trigger: event
    event_type: my_custom_event
```

### MQTT Trigger

Fires on MQTT messages.

```yaml
trigger:
  - trigger: mqtt
    topic: "zigbee2mqtt/button/action"
    payload: "single"
```

### Device Trigger (Use Sparingly)

Device triggers use device_id which is NOT persistent. Prefer state triggers.

```yaml
# Avoid when possible - device_id changes on re-add
trigger:
  - trigger: device
    domain: mqtt
    device_id: abc123
    type: action
    subtype: single
```

---

## Wait Actions

### wait_for_trigger (Preferred)

Event-driven wait. More efficient than polling.

```yaml
# Wait for door to close
- wait_for_trigger:
    - trigger: state
      entity_id: binary_sensor.door
      to: "off"
  timeout:
    minutes: 5
  continue_on_timeout: false  # Stop automation if timeout

# Wait for any of multiple triggers
- wait_for_trigger:
    - trigger: state
      entity_id: binary_sensor.door
      to: "off"
    - trigger: event
      event_type: mobile_app_notification_action
      event_data:
        action: "CLOSE_DOOR"
```

### wait_template (Use Sparingly)

Polls until template is true. **Immediately continues if already true.**

```yaml
# Only use when wait_for_trigger cannot express the condition
- wait_template: "{{ states('sensor.temperature') | float > 25 }}"
  timeout:
    minutes: 10
```

**Key difference:**
- `wait_for_trigger` waits for a **change** to occur
- `wait_template` waits for a **condition** to be true (passes immediately if already true)

### Checking Wait Results

Both waits set `wait.completed` and `wait.remaining`:

```yaml
- wait_for_trigger:
    - trigger: state
      entity_id: binary_sensor.door
      to: "off"
  timeout:
    minutes: 5

- if:
    - "{{ not wait.completed }}"
  then:
    - action: notify.mobile_app
      data:
        message: "Door still open after 5 minutes!"
```

---

## Automation Modes

The `mode` determines what happens when an automation triggers while already running.

### single (Default)

New triggers are ignored while running. A warning is logged.

**Best for:** One-shot notifications, actions that shouldn't overlap.

```yaml
automation:
  - alias: "Doorbell notification"
    mode: single
    trigger:
      - trigger: state
        entity_id: binary_sensor.doorbell
        to: "on"
    action:
      - action: notify.mobile_app
        data:
          message: "Someone at the door!"
```

### restart

Stops the current run and starts fresh. Timer-based actions are reset.

**Best for:** Motion-activated lights with timeout, retriggerable delays.

```yaml
mode: restart  # Re-trigger resets the timer
```

See `references/examples.yaml` Example 1 for a complete motion-light automation using restart + wait_for_trigger.

### queued

Queues new triggers to run after current run completes.

**Best for:** Sequential actions, door locks, garage doors.

```yaml
automation:
  - alias: "Garage door controller"
    mode: queued
    max: 5  # Maximum queue size
    trigger:
      - trigger: state
        entity_id: input_boolean.garage_door_trigger
        to: "on"
    action:
      - action: cover.toggle
        target:
          entity_id: cover.garage_door
      - delay:
          seconds: 20  # Wait for door to fully open/close
```

### parallel

Runs multiple instances simultaneously.

**Best for:** Per-entity actions with `trigger.entity_id`, notifications that shouldn't block.

```yaml
automation:
  - alias: "Window open too long"
    mode: parallel
    max: 10  # Maximum parallel runs
    trigger:
      - trigger: state
        entity_id:
          - binary_sensor.window_bedroom
          - binary_sensor.window_kitchen
          - binary_sensor.window_living
        to: "on"
        for:
          minutes: 30
    action:
      - action: notify.mobile_app
        data:
          message: "{{ trigger.to_state.name }} has been open for 30 minutes"
```

### max_exceeded

Control logging when max runs are exceeded:

```yaml
automation:
  - alias: "Quiet automation"
    mode: single
    max_exceeded: silent  # No warning logged
```

---

## if/then vs choose

### if/then/else

Use for simple binary conditions:

```yaml
actions:
  - if:
      - condition: state
        entity_id: sun.sun
        state: "below_horizon"
    then:
      - action: light.turn_on
        target:
          entity_id: light.porch
    else:
      - action: light.turn_off
        target:
          entity_id: light.porch
```

### choose

Use for multiple branches (like switch/case):

```yaml
actions:
  - choose:
      - conditions:
          - condition: trigger
            id: "morning"
        sequence:
          - action: scene.turn_on
            target:
              entity_id: scene.morning

      - conditions:
          - condition: trigger
            id: "evening"
        sequence:
          - action: scene.turn_on
            target:
              entity_id: scene.evening

    default:
      - action: light.turn_off
        target:
          area_id: living_room
```

---

## Trigger IDs

Assign IDs to triggers for use in conditions and choose:

```yaml
automation:
  - alias: "Multi-trigger automation"
    trigger:
      - trigger: state
        entity_id: binary_sensor.motion
        to: "on"
        id: "motion_on"

      - trigger: state
        entity_id: binary_sensor.motion
        to: "off"
        for:
          minutes: 5
        id: "motion_off"

    action:
      - choose:
          - conditions:
              - condition: trigger
                id: "motion_on"
            sequence:
              - action: light.turn_on
                target:
                  entity_id: light.hallway

          - conditions:
              - condition: trigger
                id: "motion_off"
            sequence:
              - action: light.turn_off
                target:
                  entity_id: light.hallway
```

Access trigger info in templates with `trigger.id`, `trigger.entity_id`, `trigger.to_state`, etc.
