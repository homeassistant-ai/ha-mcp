"""
MCP resources providing Home Assistant entity documentation.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_mcp_resources(mcp: "FastMCP") -> None:
    """Register MCP resources with real Home Assistant documentation."""

    @mcp.resource("entity-docs://media_player")
    async def media_player_documentation() -> str:
        """Real Home Assistant documentation for media_player entities."""
        return """# Media_Player Entity Control Documentation

## Description
Instructions on how to setup your media players with Home Assistant.

## Entity Pattern
All media_player entities follow the pattern: `media_player.{device_name}`

## Available Actions (32 total)

### Basic Control Actions
- **`turn_on`** - Action available for this domain
- **`turn_off`** - Action available for this domain
- **`toggle`** - Action available for this domain
- **`media_stop`** - Action available for this domain

### Configuration Actions
- **`media_player.select_source`** - 
- **`media_player.select_sound_mode`** - Currently only supported on [Denon AVR](/integrations/denonavr/) and  [Songpal](/integrations/songpal).
- **`select_source`** - Action available for this domain
- **`select_sound_mode`** - Action available for this domain

### Other Actions
- **`media_player.volume_mute`** - 
- **`media_player.volume_set`** - 
- **`media_player.media_seek`** - 
- **`media_player.play_media`** - 
- **`media_player.shuffle_set`** - 
- **`media_player.repeat_set`** - 
- **`media_player.join`** - Allows to group media players together for synchronous playback. Only works on supported multiroom audio systems.
- **`media_player.unjoin`** - 
- **`media_player.browse_media`** - Provides access to browsing the media tree provided by the integration. Similar in functionality to browsing media through the media player UI. Common use cases include automations that need to navigate media libraries and find media by specific categories.
- **`volume_up`** - Action available for this domain
- **`volume_down`** - Action available for this domain
- **`volume_set`** - Action available for this domain
- **`volume_mute`** - Action available for this domain
- **`media_play_pause`** - Action available for this domain
- **`media_play`** - Action available for this domain
- **`media_pause`** - Action available for this domain
- **`media_next_track`** - Action available for this domain
- **`media_previous_track`** - Action available for this domain
- **`clear_playlist`** - Action available for this domain
- **`shuffle_set`** - Action available for this domain
- **`repeat_set`** - Action available for this domain
- **`play_media`** - Action available for this domain
- **`join`** - Action available for this domain
- **`unjoin`** - Action available for this domain

## Usage Examples

### Example 1
```yaml
action: media_player.play_media
target:
  entity_id: media_player.chromecast
data:
  media_content_type: music
  media_content_id: "https://fake-home-assistant.io.stream/aac"
  extra:
    thumb: "https://brands.home-assistant.io/_/homeassistant/logo.png"
    title: HomeAssistantRadio
```

### Example 2
```yaml
# Get the top of the browse tree
  - action: media_player.browse_media
    target:
      entity_id: media_player.living_room
    response_variable: top_level
```

## Test Environment Entities (8 available)
- `media_player.living_room`
- `media_player.bedroom`
- `media_player.walkman`
- `media_player.kitchen`
- `media_player.lounge_room`
- `media_player.browse`
- `media_player.group`
- `media_player.search`

## AI Agent Tips
- Use `get_entity_state('entity_id')` to check current state before actions
- Use `smart_entity_search('media_player')` to find all media_player entities
- Check entity attributes for available options (e.g., fan modes, presets)
- Always specify entity_id in action calls
- Use `call_service()` with domain 'media_player' for these actions

"""

    @mcp.resource("entity-docs://climate")
    async def climate_documentation() -> str:
        """Real Home Assistant documentation for climate entities."""
        return """# Climate Entity Control Documentation

## Description
Instructions on how to setup climate control devices within Home Assistant.

## Entity Pattern
All climate entities follow the pattern: `climate.{device_name}`

## Available Actions (12 total)

### Basic Control Actions
- **`climate.turn_on`** - Turn climate device on. This is only supported if the climate device supports being turned off.
- **`climate.turn_off`** - Turn climate device off. This is only supported if the climate device has the HVAC mode `off`.
- **`climate.toggle`** - Toggle climate device. This is only supported if the climate device supports being turned on and off.

### Configuration Actions
- **`climate.set_aux_heat`** - Turn auxiliary heater on/off for climate device
- **`climate.set_preset_mode`** - Set preset mode for climate device. Away mode changes the target temperature permanently to a temperature
- **`climate.set_temperature`** - Set target temperature of climate device
- **`climate.set_humidity`** - Set target humidity of climate device
- **`climate.set_fan_mode`** - Set fan operation for climate device
- **`climate.set_hvac_mode`** - Set climate device's HVAC mode
- **`climate.set_swing_mode`** - Set swing operation mode for climate device
- **`climate.set_swing_horizontal_mode`** - Set horizontal swing operation mode for climate device

## Usage Examples

### Example 1
```yaml
automation:
  triggers:
    - trigger: time
      at: "07:15:00"
  actions:
    - action: climate.set_aux_heat
      target:
        entity_id: climate.kitchen
      data:
        aux_heat: true
```

## Test Environment Entities (3 available)
- `climate.heatpump`
- `climate.hvac`
- `climate.ecobee`

## AI Agent Tips
- Use `get_entity_state('entity_id')` to check current state before actions
- Use `smart_entity_search('climate')` to find all climate entities
- Check entity attributes for available options (e.g., fan modes, presets)
- Always specify entity_id in action calls
- Use `call_service()` with domain 'climate' for these actions

"""

    @mcp.resource("entity-docs://fan")
    async def fan_documentation() -> str:
        """Real Home Assistant documentation for fan entities."""
        return """# Fan Entity Control Documentation

## Description
Instructions on how to setup Fan devices within Home Assistant.

## Entity Pattern
All fan entities follow the pattern: `fan.{device_name}`

## Available Actions (8 total)

### Basic Control Actions
- **`fan.turn_on`** - Turn fan device on.
- **`fan.turn_off`** - Turn fan device off.

### Configuration Actions
- **`fan.set_percentage`** - Sets the speed percentage for fan device.
- **`fan.set_preset_mode`** - Sets a preset mode for the fan device.
- **`fan.set_direction`** - Sets the rotation for fan device.

### Other Actions
- **`fan.oscillate`** - Sets the oscillation for fan device.
- **`fan.increase_speed`** - Increases the speed of the fan device.
- **`fan.decrease_speed`** - Decreases the speed of the fan device.

## Test Environment Entities (5 available)
- `fan.living_room_fan`
- `fan.ceiling_fan`
- `fan.percentage_full_fan`
- `fan.percentage_limited_fan`
- `fan.preset_only_limited_fan`

## AI Agent Tips
- Use `get_entity_state('entity_id')` to check current state before actions
- Use `smart_entity_search('fan')` to find all fan entities
- Check entity attributes for available options (e.g., fan modes, presets)
- Always specify entity_id in action calls
- Use `call_service()` with domain 'fan' for these actions

"""

    @mcp.resource("entity-docs://light")
    async def light_documentation() -> str:
        """Real Home Assistant documentation for light entities."""
        return """# Light Entity Control Documentation

## Description
Instructions on how to setup light devices within Home Assistant.

## Entity Pattern
All light entities follow the pattern: `light.{device_name}`

## Available Actions

### Basic Control Actions
- **`light.turn_on`** - Turn light on with optional parameters
- **`light.turn_off`** - Turn light off
- **`light.toggle`** - Toggle light on/off

### Configuration Actions
- **`light.set_brightness`** - Set brightness level (0-255)
- **`light.set_color`** - Set RGB color
- **`light.set_color_temp`** - Set color temperature
- **`light.set_effect`** - Set light effect

## AI Agent Tips
- Use `get_entity_state('entity_id')` to check current state before actions
- Use `smart_entity_search('light')` to find all light entities
- Check entity attributes for available options (brightness, colors, effects)
- Always specify entity_id in action calls
- Use `call_service()` with domain 'light' for these actions

"""
