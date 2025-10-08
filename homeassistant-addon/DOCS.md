# Home Assistant MCP Server Add-on Documentation

## What is MCP?

Model Context Protocol (MCP) is an open protocol that enables AI assistants to securely connect to external data sources and tools. This add-on implements an MCP server specifically for Home Assistant.

## What can you do with this add-on?

With this add-on running, your AI assistant can:

- **Control Devices**: Turn lights on/off, adjust thermostats, control media players
- **Search Entities**: Find devices using fuzzy search (handles typos)
- **Manage Automations**: Create, modify, enable/disable, and trigger automations
- **Manage Scripts**: Create and run Home Assistant scripts
- **Manage Helpers**: Create and configure input_boolean, input_number, input_select, etc.
- **Create Backups**: Fast local backups before making changes
- **Query System**: Get entity states, weather, energy data, and system info

## Configuration

### backup_hint

This option controls when the MCP server suggests creating backups before operations:

- **strong**: Suggests backup before the FIRST modification of day/session (very cautious)
- **normal** (recommended): Suggests backup only before operations that CANNOT be undone (e.g., deleting devices, automations)
- **weak**: Rarely suggests backups (only when explicitly required)
- **auto**: Future intelligent detection based on operation type and backup schedule

Default: `normal`

## How to use with AI assistants

### Claude Desktop

1. Install and start this add-on
2. Configure Claude Desktop's MCP settings to connect to this add-on
3. Start chatting! Try:
   - "Turn on the living room lights"
   - "Create an automation to turn off lights at midnight"
   - "What's the current temperature?"

### Claude Code

Similar to Claude Desktop - configure the MCP client to use this add-on's stdio interface.

## Automatic Discovery

This add-on automatically:

- Discovers your Home Assistant URL
- Authenticates using Supervisor token
- Configures secure communication

No manual token configuration needed!

## Backup Feature

Backups are:
- **Fast**: Database excluded for speed
- **Local**: Stored on your Home Assistant instance
- **Encrypted**: Uses Home Assistant's default encryption password
- **Automatic**: Suggested before destructive operations

## Tools Available

The add-on provides 20+ tools including:

### Discovery & Search
- `ha_search_entities`: Fuzzy search for entities
- `ha_get_overview`: System overview optimized for AI
- `ha_get_state`: Get entity state
- `ha_get_logbook`: Query logbook entries

### Device Control
- `ha_call_service`: Call any Home Assistant service
- `ha_bulk_operate_devices`: Control multiple devices at once

### Configuration Management
- `ha_config_get_*`: Get automations, scripts, helpers
- `ha_config_set_*`: Create/update automations, scripts, helpers
- `ha_config_delete_*`: Delete automations, scripts, helpers
- `ha_trigger_automation`: Trigger automations

### Backup & Safety
- `ha_backup_create`: Create fast backup
- `ha_backup_restore`: Restore from backup (last resort)

### Convenience
- `ha_create_scene`: Create scenes from current state
- `ha_get_weather`: Get weather forecast
- `ha_get_energy_info`: Energy dashboard data
- `ha_get_docs_url`: Find relevant documentation

## Troubleshooting

### Add-on won't start

Check the add-on logs for errors. Most common issues:
- Invalid configuration in config.yaml
- Python dependency installation failures

### AI assistant can't connect

Ensure:
1. Add-on is running (check status in Add-ons page)
2. Your MCP client is configured correctly
3. Check add-on logs for connection attempts

### Operations failing

Check add-on logs for detailed error messages. The add-on sanitizes errors to prevent token leakage while maintaining usefulness.

## Support & Development

- GitHub: https://github.com/homeassistant-ai/ha-mcp
- Issues: https://github.com/homeassistant-ai/ha-mcp/issues
- Wiki: https://github.com/homeassistant-ai/ha-mcp/wiki
