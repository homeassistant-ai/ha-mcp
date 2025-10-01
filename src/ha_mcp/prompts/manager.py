"""
MCP prompts providing domain-specific guidance for AI agents.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_mcp_prompts(mcp: "FastMCP") -> None:
    """Register MCP prompts with practical usage patterns."""

    @mcp.prompt("media-control-guide")
    async def media_control_guide(query: str = "") -> str:
        """Media Player Control Assistant"""
        return """You are helping control media players in Home Assistant.

Available Actions (32 total):
- media_player.volume_mute: 
- media_player.volume_set: 
- media_player.media_seek: 
- media_player.play_media: 
- media_player.select_source: 
- media_player.select_sound_mode: Currently only supported on [Denon AVR](/integrations/denonavr/) and  [Songpal](/integrations/songpal).
- media_player.shuffle_set: 
- media_player.repeat_set: 

Available Entities:
- media_player.living_room
- media_player.bedroom
- media_player.walkman
- media_player.kitchen
- media_player.lounge_room
- media_player.browse
- media_player.group
- media_player.search

Common Controls:
- **Playback**: media_player.play, media_player.pause, media_player.stop
- **Volume**: media_player.volume_set (0.0-1.0), media_player.volume_mute
- **Navigation**: media_player.media_previous_track, media_player.media_next_track
- **Content**: media_player.play_media (with media_content_id and media_content_type)

Example Usage:
```python
# Play Spotify playlist
call_service("media_player", "play_media",
            entity_id="media_player.living_room",
            media_content_id="spotify:playlist:123456",
            media_content_type="playlist")
```
"""

    @mcp.prompt("climate-control-guide")
    async def climate_control_guide(query: str = "") -> str:
        """Climate Control Assistant"""
        return """You are helping control climate/HVAC devices in Home Assistant.

Available Actions:
- climate.set_aux_heat: Turn auxiliary heater on/off for climate device
- climate.set_preset_mode: Set preset mode for climate device. Away mode changes the target temperature permanently to a temperature
- climate.set_temperature: Set target temperature of climate device
- climate.set_humidity: Set target humidity of climate device
- climate.set_fan_mode: Set fan operation for climate device
- climate.set_hvac_mode: Set climate device's HVAC mode

Available Entities:
- climate.heatpump
- climate.hvac
- climate.ecobee

Common Scenarios:
1. **Set Temperature**: Use `climate.set_temperature` with temperature and hvac_mode
2. **Change Mode**: Use `climate.set_hvac_mode` (heat, cool, auto, off)
3. **Set Preset**: Use `climate.set_preset_mode` (away, eco, home, sleep)
4. **Fan Control**: Use `climate.set_fan_mode` (auto, low, medium, high)

Example Usage:
```python
# Set living room to 72°F in heat mode
call_service("climate", "set_temperature", 
            entity_id="climate.living_room", 
            temperature=72, hvac_mode="heat")
```

Always check current state first with `get_entity_state()` to understand current settings.
"""

    @mcp.prompt("ha-entity-control-guide")
    async def ha_entity_control_guide(query: str = "") -> str:
        """Home Assistant Entity Control Guide"""
        return """You are helping control Home Assistant entities. Here's your systematic approach:

## Step 1: Discover Entities
- Use `smart_entity_search("keyword")` to find entities
- Use `get_entities_by_area("room")` for room-based control
- Use `get_system_overview()` to understand available domains

## Step 2: Understand Current State  
- Always use `get_entity_state("entity_id")` first
- Check entity attributes for available options
- Look at current state before making changes

## Step 3: Choose Right Action
Top domains by available actions:
- media_player: 32 actions (8 entities)
- climate: 12 actions (3 entities)
- fan: 8 actions (5 entities)
- light: 8+ actions (many entities)

## Step 4: Execute Action
- Use `call_service(domain, service, entity_id="...", **parameters)`
- Check `@mcp.resource("entity-docs://domain")` for domain-specific help
- Verify result with another `get_entity_state()` call

## Error Prevention
- Always specify entity_id
- Check entity exists before controlling
- Use correct parameter names and values
- Handle domains with no actions (like sensor) appropriately
"""
