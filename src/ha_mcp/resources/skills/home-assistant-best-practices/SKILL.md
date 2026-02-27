---
name: home-assistant-best-practices
description: >
  Best practices for Home Assistant automations, helpers, scripts, and device controls.

  TRIGGER THIS SKILL WHEN:
  - Creating or editing HA automations, scripts, or scenes
  - Choosing between template sensors and built-in helpers
  - Writing or restructuring triggers, conditions, or automation modes
  - Setting up Zigbee button/remote automations (ZHA or Zigbee2MQTT)
  - Renaming entities or migrating device_id references to entity_id

  SYMPTOMS THAT TRIGGER THIS SKILL:
  - Agent uses Jinja2 templates where native conditions, triggers, or helpers exist
  - Agent uses device_id instead of entity_id in triggers/actions
  - Agent modifies entity IDs or config objects without checking all consumers
  - Agent chooses wrong automation mode (e.g., single for motion lights)
metadata:
  version: "1.0.0"
---

# Home Assistant Best Practices

**Core principle:** Use native Home Assistant constructs wherever possible. Templates bypass validation, fail silently at runtime, and make debugging opaque.

## Decision Workflow

Follow this sequence when creating any automation:

### 0. Gate: modifying existing config?

If your change affects entity IDs or cross-component references — renaming entities, replacing template sensors with helpers, converting device triggers, or restructuring automations — read `references/safe-refactoring.md` first. That reference covers impact analysis, device-sibling discovery, and post-change verification. Complete its workflow before proceeding.

Steps 1-5 below apply to new config or pattern evaluation.

### 1. Check for native condition/trigger
Before writing any template, check `references/automation-patterns.md` for native alternatives.

**Common substitutions:**
- `{{ states('x') | float > 25 }}` → `numeric_state` condition with `above: 25`
- `{{ is_state('x', 'on') and is_state('y', 'on') }}` → `condition: and` with state conditions
- `{{ now().hour >= 9 }}` → `condition: time` with `after: "09:00:00"`
- `wait_template: "{{ is_state(...) }}"` → `wait_for_trigger` with state trigger (caveat: different behavior when state is already true — see `references/safe-refactoring.md#trigger-restructuring`)

### 2. Check for built-in helper
Before creating a template sensor, check `references/helper-selection.md`.

**Common substitutions:**
- Sum/average multiple sensors → `min_max` integration
- Binary any-on/all-on logic → `group` helper
- Rate of change → `derivative` integration
- Cross threshold detection → `threshold` integration
- Consumption tracking → `utility_meter` helper

### 3. Select correct automation mode
Default `single` mode is often wrong. See `references/automation-patterns.md#automation-modes`.

| Scenario | Mode |
|----------|------|
| Motion light with timeout | `restart` |
| Sequential processing (door locks) | `queued` |
| Independent per-entity actions | `parallel` |
| One-shot notifications | `single` |

### 4. Use entity_id over device_id
`device_id` breaks when devices are re-added. See `references/device-control.md`.

**Exception:** Zigbee2MQTT autodiscovered device triggers are acceptable.

### 5. For Zigbee buttons/remotes
- **ZHA:** Use `event` trigger with `device_ieee` (persistent)
- **Z2M:** Use `device` trigger (autodiscovered) or `mqtt` trigger

See `references/device-control.md#zigbee-buttonremote-patterns`.

---

## Critical Anti-Patterns

| Anti-pattern | Use instead | Why | Reference |
|---|---|---|---|
| `condition: template` with `float > 25` | `condition: numeric_state` | Validated at load, not runtime | `references/automation-patterns.md#native-conditions` |
| `wait_template: "{{ is_state(...) }}"` | `wait_for_trigger` with state trigger | Event-driven, not polling; waits for *change* (see `references/safe-refactoring.md#trigger-restructuring` for semantic differences) | `references/automation-patterns.md#wait-actions` |
| `device_id` in triggers | `entity_id` (or `device_ieee` for ZHA) | device_id breaks on re-add | `references/device-control.md#entity-id-vs-device-id` |
| `mode: single` for motion lights | `mode: restart` | Re-triggers must reset the timer | `references/automation-patterns.md#automation-modes` |
| Template sensor for sum/mean | `min_max` helper | Declarative, handles unavailable states | `references/helper-selection.md#numeric-aggregation` |
| Template binary sensor with threshold | `threshold` helper | Built-in hysteresis support | `references/helper-selection.md#threshold` |
| Renaming entity IDs without impact analysis | Follow `references/safe-refactoring.md` workflow | Renames break dashboards, scripts, and scenes silently | `references/safe-refactoring.md#entity-renames` |

---

## Reference Files

Read these when you need detailed information:

| File | When to read | Key sections |
|------|--------------|--------------|
| `references/safe-refactoring.md` | Renaming entities, replacing helpers, restructuring automations, or any modification to existing config | `#universal-workflow`, `#entity-renames`, `#helper-replacements`, `#trigger-restructuring` |
| `references/automation-patterns.md` | Writing triggers, conditions, waits, or choosing automation modes | `#native-conditions`, `#trigger-types`, `#wait-actions`, `#automation-modes`, `#ifthen-vs-choose`, `#trigger-ids` |
| `references/helper-selection.md` | Deciding whether to use a built-in helper vs template sensor | `#numeric-aggregation`, `#rate-and-change`, `#time-based-tracking`, `#counting-and-timing`, `#scheduling`, `#entity-grouping`, `#decision-matrix` |
| `references/template-guidelines.md` | Confirming templates ARE appropriate for a use case | `#when-templates-are-appropriate`, `#when-to-avoid-templates`, `#template-sensor-best-practices`, `#common-patterns`, `#error-handling` |
| `references/device-control.md` | Writing service calls, Zigbee button automations, or using target: | `#entity-id-vs-device-id`, `#service-calls-best-practices`, `#zigbee-buttonremote-patterns`, `#domain-specific-patterns` |
| `references/examples.yaml` | Need compound examples combining multiple best practices | — |
