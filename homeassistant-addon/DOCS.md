# Home Assistant MCP Server Add-on

AI assistant integration for Home Assistant via Model Context Protocol (MCP).

## Capabilities

With this add-on, your AI assistant can:

- **Control devices**: Lights, thermostats, media players, and more
- **Manage automations and scripts**: Create, modify, enable/disable, trigger
- **Manage helpers**: input_boolean, input_number, input_select, input_text, input_datetime, input_button
- **Search entities**: Fuzzy search handles typos
- **Create backups**: Fast local backups before destructive operations
- **Query system**: Entity states, weather, energy data, logbook history

For complete features and tool reference, see:
https://github.com/homeassistant-ai/ha-mcp

## Configuration

### backup_hint

Controls when the MCP server suggests creating backups before operations:

- **strong**: Suggests backup before the FIRST modification of day/session (very cautious)
- **normal**: Suggests backup only before operations that CANNOT be undone (default, recommended)
- **weak**: Rarely suggests backups (only when explicitly required)
- **auto**: Future intelligent detection based on operation type

Default: `normal`

## Usage

Once started, the add-on runs an MCP server that AI assistants connect to via stdio.

**Zero configuration required** - the add-on automatically:
- Discovers your Home Assistant URL
- Authenticates using Supervisor token
- Configures secure communication

For AI assistant setup (Claude Desktop, Claude Code, etc.), see:
https://github.com/homeassistant-ai/ha-mcp#client-configuration

## Troubleshooting

### Add-on won't start

Check the add-on logs for errors. Common issues:
- Invalid configuration in config.yaml
- Python dependency installation failures

### AI assistant can't connect

Verify:
1. Add-on is running (check status in Add-ons page)
2. Your MCP client is configured correctly
3. Check add-on logs for connection attempts

### Operations failing

Check add-on logs for detailed error messages. The add-on sanitizes errors to prevent token leakage while maintaining usefulness.

## Support

- **Issues**: https://github.com/homeassistant-ai/ha-mcp/issues
- **Documentation**: https://github.com/homeassistant-ai/ha-mcp
- **Wiki**: https://github.com/homeassistant-ai/ha-mcp/wiki
