# mypy: disable-error-code="name-defined"
"""
Enhanced MCP Prompts for Home Assistant

Domain-specific prompts based on real HA integration documentation
to guide AI agents for successful interactions.
"""

from typing import Annotated, Any

from pydantic import Field


class EnhancedPromptsMixin:
    """Mixin class to add enhanced prompts to existing MCP server."""

    mcp: Any  # MCP server instance, will be available when mixed in

    def register_enhanced_prompts(self) -> None:
        """Register all enhanced domain-specific prompts."""
        self._register_control_prompts()
        self._register_input_helper_prompts()
        self._register_monitoring_prompts()
        self._register_automation_prompts()
        self._register_general_guidance_prompts()

    def _register_control_prompts(self) -> None:
        """Register prompts for control domains."""

        @self.mcp.prompt(
            name="light-control-expert",
            description="Expert guidance for Home Assistant lighting control",
        )
        async def light_control_expert(
            lighting_intent: Annotated[
                str, Field(description="What user wants to do with lights")
            ],
            room: Annotated[str | None, Field(description="Room or area name")] = None,
        ) -> str:
            """Expert lighting control guidance."""

            return f"""# Light Control Expert Assistant

## User Intent: {lighting_intent}
{f"## Target Room: {room}" if room else ""}

## Available Light Actions
- **light.turn_on** - Turn on with optional parameters
- **light.turn_off** - Turn off with optional transition
- **light.toggle** - Switch current state

## Parameter Reference

### Brightness Control
- **brightness_pct**: 0-100% (user-friendly percentage)
- **brightness**: 0-255 (absolute value)
- **brightness_step_pct**: ±percentage adjustment

### Color Control
- **color_temp_kelvin**: 2000K (warm) to 6500K (cool)
- **rgb_color**: [red, green, blue] values 0-255 each
- **color_name**: "red", "blue", "warm_white", "cool_white"

### Advanced Options
- **transition**: Time in seconds for smooth changes
- **effect**: "colorloop", "random" (device-dependent)

## Intent Recognition Examples

### Brightness Control
```python
# "Make it brighter" or "50% brightness"
call_service_enhanced("light", "turn_on", entity_id=f"light.{room or "bedroom"}", 
                    brightness_pct=75)

# "Dim the lights"
call_service_enhanced("light", "turn_on", entity_id=f"light.{room or "living_room"}", 
                    brightness_pct=25)
```

### Color/Temperature Control
```python
# "Warm white" or "reading light"
call_service_enhanced("light", "turn_on", entity_id=f"light.{room or "bedroom"}", 
                    color_temp_kelvin=2700, brightness_pct=80)

# "Red light" or "romantic lighting"
call_service_enhanced("light", "turn_on", entity_id=f"light.{room or "living_room"}", 
                    color_name="red", brightness_pct=30)
```

## Best Practices
1. Check capabilities: get_entity_state_comprehensive() shows supported_color_modes
2. Use brightness_pct (0-100) instead of brightness (0-255) for user-friendly control
3. Add transition=2 for smooth changes
4. Color temp guide: 2700K=warm, 4000K=neutral, 6500K=cool
5. Need hardware-specific parameters? Call ha_get_domain_docs("light") for the latest integration guidance

## Troubleshooting
- Unsupported feature: Check entity's supported_color_modes
- Color not working: Verify entity supports rgb or color_temp
- Invalid brightness: Ensure 0-100% range
"""

        @self.mcp.prompt(
            name="climate-control-expert",
            description="Expert guidance for HVAC and thermostat control",
        )
        async def climate_control_expert(
            climate_intent: Annotated[
                str, Field(description="What user wants to do with climate")
            ],
            room: Annotated[str | None, Field(description="Room or area name")] = None,
        ) -> str:
            """Expert climate control guidance."""

            return f"""# Climate Control Expert Assistant

## User Intent: {climate_intent}
{f"## Target Room: {room}" if room else ""}

## Available Climate Actions
- **climate.set_temperature** - Set target temperature
- **climate.set_hvac_mode** - Change heating/cooling mode
- **climate.set_preset_mode** - Use energy-saving presets
- **climate.set_fan_mode** - Control fan speed

## HVAC States & Modes
- **States**: "heat", "cool", "auto", "off", "dry", "fan_only"
- **Preset Modes**: "home", "away", "eco", "sleep", "comfort"
- **Fan Modes**: "auto", "low", "medium", "high"

## Intent Recognition Examples

### Temperature Adjustment
```python
# "Make it warmer" (relative adjustment)
# First get current temp, then adjust
current_state = get_entity_state_comprehensive(f"climate.{room or "thermostat"}")
current_temp = current_state.get("attributes", {{}}).get("temperature", 20)
call_service_enhanced("climate", "set_temperature", 
                    entity_id=f"climate.{room or "thermostat"}", 
                    temperature=current_temp + 2)

# "Set to 72 degrees" (absolute temperature)
call_service_enhanced("climate", "set_temperature", 
                    entity_id=f"climate.{room or "thermostat"}", 
                    temperature=22)  # 72°F ≈ 22°C
```

### Mode Control
```python
# "Turn on heat" or "heating mode"
call_service_enhanced("climate", "set_hvac_mode", 
                    entity_id=f"climate.{room or "thermostat"}", 
                    hvac_mode="heat")

# "Eco mode" or "energy saving"
call_service_enhanced("climate", "set_preset_mode", 
                    entity_id=f"climate.{room or "thermostat"}", 
                    preset_mode="eco")
```

## Best Practices
1. Check current state first with get_entity_state_comprehensive()
2. Validate temperature range using min_temp/max_temp attributes
3. Use gradual adjustments (±2-3 degrees) for efficiency
4. Consider presets for common scenarios (away, sleep, eco)
5. Pull official references with ha_get_domain_docs("climate") when unsure about supported modes

## Parameter Validation
- **temperature**: Must be within entity's min_temp to max_temp
- **hvac_mode**: Check hvac_modes attribute for valid options
- **preset_mode**: Check preset_modes attribute for available presets
"""

    def _register_input_helper_prompts(self) -> None:
        """Register prompts for input helper domains."""

        @self.mcp.prompt(
            name="input-helper-expert",
            description="Expert guidance for Home Assistant input helpers",
        )
        async def input_helper_expert(
            helper_intent: Annotated[
                str, Field(description="What user wants to do with input helpers")
            ],
            helper_type: Annotated[
                str | None, Field(description="Type of input helper")
            ] = None,
        ) -> str:
            """Expert input helper control guidance."""

            return f"""# Input Helper Expert Assistant

## User Intent: {helper_intent}
{f"## Helper Type: {helper_type}" if helper_type else ""}

## Input Helper Types

### input_number (Sliders/Numeric Controls)
**Use Cases**: Temperature setpoints, volume levels, timer durations, thresholds
**Actions**: set_value, increment, decrement
**Example**:
```python
# Set temperature setpoint
call_service_enhanced("input_number", "set_value", 
                    entity_id="input_number.temp_setpoint", value=22)

# Increment by step
call_service_enhanced("input_number", "increment", 
                    entity_id="input_number.volume_level")
```

### input_boolean (Toggle Switches)
**Use Cases**: Guest mode, vacation mode, automation overrides, feature flags

## Automation Configuration Management

For automation CRUD operations, use these tools:

### List Automations
```python
# Find all automations (they are entities)
automations = ha_search_entities(query="", domain_filter="automation")

# Get automation runtime state (enabled/disabled)
for auto in automations["entities"]:
    state = ha_get_state(auto["entity_id"])
    print(f"{auto["friendly_name"]}: {state["state"]}")
```

### Get Automation Configuration
```python
# Get full automation configuration by entity_id (recommended)
config = ha_get_automation_config("automation.morning_routine")

# Or get by unique_id if known
config = ha_get_automation_config("1234567890123")
```

### Create/Update Automation
```python
# Create new automation (unique_id auto-generated)
result = ha_upsert_automation({
                "alias": "My New Automation",
    "description": "Automation description", 
    "triggers": [{"platform": "time", "at": "07:00:00"}],
    "actions": [{"service": "light.turn_on", "entity_id": "light.bedroom"}]
})

# Update existing automation by entity_id (recommended)
result = ha_upsert_automation({
                "alias": "Updated Automation",
    "triggers": [{"platform": "time", "at": "08:00:00"}],
    "actions": [{"service": "light.turn_off", "entity_id": "light.bedroom"}]
}, identifier="automation.morning_routine")

# Or update by unique_id
result = ha_upsert_automation({...}, identifier="1234567890123")
```

### Delete Automation
```python
# Delete automation by entity_id (recommended)
result = ha_delete_automation("automation.morning_routine")

# Or delete by unique_id
result = ha_delete_automation("1234567890123")
```

**Note**: Automation configuration management is separate from runtime control:
- **Configuration**: ha_get_automation_config, ha_upsert_automation, ha_delete_automation
- **Runtime Control**: ha_trigger_automation, ha_call_service("automation", "turn_on/turn_off")
- **State Monitoring**: ha_get_state for enabled/disabled status

**Parameter Support**: All automation CRUD tools accept both:
- **entity_id**: `automation.morning_routine` (recommended - user-friendly)
- **unique_id**: `1234567890123` (internal configuration ID)
**Actions**: turn_on, turn_off, toggle
**Example**:
```python
# Enable guest mode
call_service_enhanced("input_boolean", "turn_on", 
                    entity_id="input_boolean.guest_mode")

# Toggle vacation mode
call_service_enhanced("input_boolean", "toggle", 
                    entity_id="input_boolean.vacation_mode")
```

### input_text (Text Input)
**Use Cases**: Status messages, dynamic device names, user notes
**Actions**: set_value
**Example**:
```python
# Set status message
call_service_enhanced("input_text", "set_value", 
                    entity_id="input_text.house_status", 
                    value="Away until Sunday")
```

### input_datetime (Date/Time Pickers)
**Use Cases**: Alarm times, event schedules, maintenance reminders
**Actions**: set_datetime
**Example**:
```python
# Set morning alarm
call_service_enhanced("input_datetime", "set_datetime", 
                    entity_id="input_datetime.morning_alarm", 
                    datetime="07:30:00")
```

### input_select (Dropdown Selections)
**Use Cases**: Mode selection, option lists, state selection
**Actions**: select_option
**Example**:
```python
# Select house mode
call_service_enhanced("input_select", "select_option", 
                    entity_id="input_select.house_mode", 
                    option="Away")
```

## Intent Recognition Patterns

### "Set [number] to [value]"
```python
# For temperature setpoint, volume, etc.
call_service_enhanced("input_number", "set_value", 
                    entity_id="input_number.target_name", value=desired_value)
```

### "Enable/Disable [feature]"
```python
# For boolean toggles
call_service_enhanced("input_boolean", "turn_on", 
                    entity_id="input_boolean.feature_name")
```

### "Set message to [text]"
```python
# For text input
call_service_enhanced("input_text", "set_value", 
                    entity_id="input_text.message_name", value="text_content")
```

## Best Practices
1. Check current values with get_entity_state_comprehensive()
2. Validate ranges for input_number (min/max attributes)
3. Check available options for input_select
4. Use descriptive names for input helpers
5. Consider automation integration when setting values

## Common Use Cases
- **Home Modes**: input_select for "Home", "Away", "Sleep", "Party"
- **Overrides**: input_boolean for "Manual Override", "Guest Mode"
- **Setpoints**: input_number for temperature, brightness, volume preferences
- **Schedules**: input_datetime for alarm times, event scheduling
- **Status**: input_text for house status messages, notes
"""

    def _register_monitoring_prompts(self) -> None:
        """Register prompts for monitoring domains."""

        @self.mcp.prompt(
            name="sensor-monitoring-expert",
            description="Expert guidance for sensor data and monitoring",
        )
        async def sensor_monitoring_expert(
            monitoring_intent: Annotated[
                str, Field(description="What user wants to know from sensors")
            ],
            sensor_type: Annotated[
                str | None, Field(description="Type of sensor")
            ] = None,
        ) -> str:
            """Expert sensor monitoring guidance."""

            return f"""# Sensor Monitoring Expert Assistant

## User Intent: {monitoring_intent}
{f"## Sensor Type: {sensor_type}" if sensor_type else ""}

## Sensor Device Classes

### Environmental Sensors
- **temperature**: Temperature readings (°C, °F)
- **humidity**: Relative humidity (%)
- **pressure**: Atmospheric pressure (hPa, mmHg)
- **air_quality**: Air quality index

### Energy & Power Sensors
- **power**: Current power consumption (W)
- **energy**: Cumulative energy usage (kWh)
- **voltage**: Electrical voltage (V)
- **current**: Electrical current (A)

### Status Sensors
- **battery**: Battery level (%)
- **signal_strength**: WiFi/cellular signal
- **connectivity**: Connection status

## Intent Recognition Examples

### "What's the temperature?"
```python
# Find temperature sensors
temp_sensors = smart_entity_search_enhanced("temperature", domain_filter="sensor")

# Get specific room temperature
room_temp = get_entity_state_comprehensive(f"sensor.{room}_temperature")
```

### "How much power is being used?"
```python
# Find power consumption sensors
power_sensors = smart_entity_search_enhanced("power consumption", domain_filter="sensor")

# Check total power usage
total_power = get_entity_state_comprehensive("sensor.total_power")
```

### "Check all battery levels"
```python
# Find all battery sensors
battery_sensors = smart_entity_search_enhanced("battery", domain_filter="sensor")

# Get low battery devices
for sensor in battery_sensors:
    state = get_entity_state_comprehensive(sensor["entity_id"])
    if float(state.get("state", 100)) < 20:
        print(f"Low battery: {sensor["friendly_name"]}")
```

## Reading Sensor Data
- **state**: Current value (numerical or text)
- **unit_of_measurement**: Units for context (°C, %, W, kWh)
- **device_class**: Sensor purpose and type
- **state_class**: measurement, total, total_increasing
- **precision**: Decimal places for accuracy

## Binary Sensors (on/off detection)
- **motion**: Movement detection
- **door/window**: Open/closed status
- **connectivity**: Online/offline status
- **battery**: Low battery warnings

## Best Practices
1. Check unit_of_measurement for proper context
2. Use device_class to understand sensor purpose
3. Monitor last_updated for data freshness
4. Consider historical data for trends
5. Set up alerts for critical thresholds

## Common Monitoring Patterns
- **Climate**: Temperature, humidity every 1-5 minutes
- **Energy**: Power real-time, energy cumulative totals
- **Security**: Motion, door/window binary sensors
- **Health**: Battery levels, connectivity status
"""

    def _register_automation_prompts(self) -> None:
        """Register prompts for automation domains."""

        @self.mcp.prompt(
            name="automation-expert",
            description="Expert guidance for Home Assistant automation control",
        )
        async def automation_expert(
            automation_intent: Annotated[
                str, Field(description="What user wants to do with automations")
            ],
            automation_name: Annotated[
                str | None, Field(description="Specific automation")
            ] = None,
        ) -> str:
            """Expert automation control guidance."""

            return f"""# Automation Expert Assistant

## User Intent: {automation_intent}
{f"## Target Automation: {automation_name}" if automation_name else ""}

## Automation Control Actions
- **automation.trigger** - Manually run automation now
- **automation.turn_on** - Enable automation (will trigger on conditions)
- **automation.turn_off** - Disable automation (won't trigger)
- **automation.toggle** - Switch enabled/disabled state

## Scene Control Actions
- **scene.turn_on** - Activate scene (restore saved states)
- **scene.create** - Create new scene from current states

## Script Control Actions
- **script.turn_on** - Execute script sequence
- **script.turn_off** - Stop running script (if applicable)

## Intent Recognition Examples

### "Run morning routine"
```python
# Manually trigger automation
call_service_enhanced("automation", "trigger", 
                    entity_id="automation.morning_routine")

# Or activate morning scene
call_service_enhanced("scene", "turn_on", 
                    entity_id="scene.morning")
```

### "Disable vacation mode automation"
```python
# Turn off automation
call_service_enhanced("automation", "turn_off", 
                    entity_id="automation.vacation_mode")
```

### "Movie night scene"
```python
# Activate predefined scene
call_service_enhanced("scene", "turn_on", 
                    entity_id="scene.movie_night")
```

### "Execute bedtime script"
```python
# Run script sequence
call_service_enhanced("script", "turn_on", 
                    entity_id="script.bedtime_routine")
```

## Understanding the Differences

### Automation vs Scene vs Script
- **Automation**: Event-driven rules (triggers → conditions → actions)
- **Scene**: Snapshot of device states (lights, switches, etc.)
- **Script**: Sequence of actions (like a manual automation)

### When to Use Each
- **Trigger automation**: Run complex logic with conditions
- **Activate scene**: Instantly set multiple devices to specific states
- **Execute script**: Run a sequence of actions manually

## Best Practices
1. **Check state first**: Verify automation exists and is enabled
2. **Understand impact**: Know what automation/scene/script does before triggering
3. **Use descriptive names**: Makes finding and controlling easier
4. **Scene creation**: Set devices manually first, then create scene
5. **Script testing**: Test scripts in safe environment first

## Safety Considerations
- Only trigger automations you understand
- Some automations are critical for home function
- Scenes immediately change device states
- Scripts may have long-running actions

## Troubleshooting
- **Automation not found**: Check exact entity_id spelling
- **Won't trigger**: Verify automation is enabled (turned on)
- **Unexpected behavior**: Check automation conditions and triggers
- **Scene issues**: Verify devices in scene still exist
"""

    def _register_general_guidance_prompts(self) -> None:
        """Register general guidance prompts."""

        @self.mcp.prompt(
            name="home-assistant-control-master",
            description="Master guidance for comprehensive Home Assistant control",
        )
        async def home_assistant_control_master(
            user_intent: Annotated[
                str, Field(description="What user wants to accomplish")
            ],
            context: Annotated[
                str | None, Field(description="Additional context")
            ] = None,
        ) -> str:
            """Master control guidance for Home Assistant."""

            return f"""# Home Assistant Control Master

## User Intent: {user_intent}
{f"## Context: {context}" if context else ""}

## Systematic Approach for AI Agents

### Step 1: Discovery & Understanding
```python
# Find relevant entities
entities = smart_entity_search_enhanced("search_term", domain_filter="domain_type")

# Understand current state
state = get_entity_state_comprehensive("entity.id")

# Get room/area overview if needed
area_info = get_entities_by_area_enhanced("room_name")
```

### Step 2: Choose Right Tool for Domain
**Control Domains**: Use call_service_enhanced()
- light, switch, climate, media_player, lock, cover, fan, vacuum

**Input Helpers**: Use call_service_enhanced() 
- input_number, input_boolean, input_text, input_datetime, input_select

**Automation**: Use call_service_enhanced()
- automation (trigger/enable/disable)
- scene (activate)
- script (execute)

**Monitoring**: Use get_entity_state_comprehensive()
- sensor, binary_sensor (read-only)

### Step 3: Domain-Specific Guidance
- **@mcp.prompt("light-control-expert")** - For lighting scenarios
- **@mcp.prompt("climate-control-expert")** - For temperature control
- **@mcp.prompt("input-helper-expert")** - For user inputs and toggles
- **@mcp.prompt("sensor-monitoring-expert")** - For data monitoring
- **@mcp.prompt("automation-expert")** - For automation/scene/script control
- **ha_get_domain_docs("<domain>")** - Fetch live Home Assistant docs when you need authoritative parameters

### Step 4: Parameter Validation
1. Check entity capabilities with get_entity_state_comprehensive()
2. Validate parameter ranges from entity attributes
3. Use proper parameter formats for domain
4. Include error handling for invalid requests

### Step 5: Execute with Confidence
```python
# Use enhanced service call with automatic error documentation
result = call_service_enhanced("domain", "service", 
                              entity_id="entity.id", 
                              **validated_parameters)
```

## Common Intent Patterns

### Room Control
"Control bedroom lights" → light domain + area filter
"Set living room temperature" → climate domain + area filter

### Device-Specific Control  
"Turn on kitchen lights" → specific entity control
"Set thermostat to 72" → specific temperature control

### Mode/State Changes
"Enable guest mode" → input_boolean toggle
"Set house to away mode" → input_select or scene activation

### Monitoring & Status
"Check temperature" → sensor readings
"What's the energy usage?" → sensor monitoring

### Automation Management
"Run morning routine" → automation trigger or scene activation
"Disable security system" → automation control

## Error Recovery Strategy
1. **Service not found**: Get domain documentation and try alternatives
2. **Entity not found**: Use smart search to find correct entity
3. **Invalid parameters**: Check entity attributes for valid ranges
4. **Permission denied**: Verify Home Assistant connection and entity access

## Best Practices Summary
- Always discover before controlling
- Check current state before making changes  
- Use domain-specific prompts for complex scenarios
- Validate parameters against entity capabilities
- Handle errors gracefully with helpful suggestions
- Prefer enhanced tools over basic ones for better error handling
"""
