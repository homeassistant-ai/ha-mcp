# Safe Refactoring Workflow

Follow this workflow whenever you modify existing Home Assistant configuration: renaming entities, replacing template sensors with helpers, converting device triggers, or restructuring automations.

**Core rule:** Search all consumers BEFORE changing anything. Verify zero stale references AFTER.

---

## Universal Workflow

### Step 1: Identify the full scope of change

Answer three questions before touching anything:

1. **What changes?** Entity ID, automation structure, sensor type, or trigger semantics.
2. **What sibling entities share the same device?** Query the device to list every entity it owns (battery sensor, update entity, diagnostic button). Plan changes for all siblings together.
   - *Tool hint:* `ha_get_device(entity_id="...")` if available, or inspect Settings > Devices.
3. **Rename one entity or all device entities?** Devices bundle 2-6 entities. Renaming the primary but leaving siblings with the old naming scheme creates inconsistency.

### Step 2: Search ALL consumers

Search every component type that references entity IDs. Do not limit searches to the component you are editing.

| Component | How to search |
|-----------|---------------|
| Automations | `ha_deep_search(query="entity_id")` or grep `automations.yaml` |
| Dashboards | `ha_dashboard_find_card(entity_id="...")` or grep `.storage/lovelace*`, `ui-lovelace.yaml` |
| Scripts | grep `scripts.yaml` |
| Scenes | grep `scenes.yaml` |
| Other | Check AppDaemon apps, Node-RED flows, Pyscript scripts, or any custom integration that references entity IDs |

Record every location found. This list becomes your update checklist for Step 4.

### Step 3: Make the change

Rename the entity, replace the template sensor, or restructure the automation.

### Step 4: Update every consumer

Work through each location from your Step 2 checklist. Update every reference to the new entity ID, helper entity, or automation structure.

### Step 5: Verify

1. **Search for the OLD identifier** across all component types. Expect zero results.
   - *Tool hint:* `ha_search_entities(query="old_name")` or grep all config files.
2. **Search for the NEW identifier** to confirm all expected locations reference it.
3. **Reload or check dashboards** if entity IDs changed.
4. **If stale references remain that you cannot update**, rename the entity back to its original ID to restore functionality, then report the blocking locations to the user.

---

## Entity Renames

Additional requirements beyond the universal workflow:

**Device-sibling discovery (Step 1):**
HA devices bundle multiple entities. A smart plug might expose `switch.*`, `sensor.*_energy`, and `update.*`. A multi-sensor exposes motion, temperature, illuminance, and battery entities. Rename all siblings to match.

Example — renaming a smart plug's entities from manufacturer defaults to room-based names:

| Domain | Old entity ID | New entity ID |
|---|---|---|
| switch | `switch.shellyplug_s_a1b2c3d4e5f6` | `switch.office_heater` |
| sensor | `sensor.shellyplug_s_a1b2c3d4e5f6_energy` | `sensor.office_heater_energy` |
| update | `update.shellyplug_s_a1b2c3d4e5f6` | `update.office_heater` |

**Dashboard reference locations (Step 2):**
Dashboard cards reference entities in multiple places. Search all of these:

- `entity:` field
- `tap_action` and `hold_action` targets
- Conditional card conditions
- Template card Jinja2 blocks

---

## Helper Replacements

When replacing a template sensor with a built-in helper (`min_max`, `threshold`, `derivative`):

**New entity ID (Step 1):**
The helper creates a new entity with a different entity_id. The old template sensor's entity_id stops existing. Update every consumer of the old entity_id to reference the new one.

**Test equivalence (Step 5):**
Verify the new helper produces the same values as the old template sensor. Check units, precision, and unavailable-state handling.

---

## Trigger Restructuring

When converting `device_id` triggers to `entity_id` triggers, or replacing `wait_template` with `wait_for_trigger`:

**Behavioral equivalence (Step 1):**
`wait_for_trigger` waits for a state *change*; `wait_template` polls for *current state*. These differ when the target state is already true at wait start: `wait_for_trigger` blocks indefinitely, `wait_template` returns immediately.

**Automation callers (Step 2):**
Search for scripts or other automations that call the automation you are restructuring via `automation.trigger` or `automation.turn_on`. Renaming or splitting an automation changes its entity_id and breaks these callers.
