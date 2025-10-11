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

### secret_path (Advanced)

**Hidden option** - requires enabling "Advanced" mode in the configuration UI.

Override the auto-generated secret path with a custom value. Leave empty for automatic generation (recommended).

- **Empty (default)**: Generates a secure 128-bit random path on first start
- **Custom value**: Use your own path for migration or specific requirements
- **Persistence**: The path is saved to `/data/secret_path.txt` and reused on restarts

## Usage

Once started, the add-on runs an MCP server accessible via HTTP with an auto-generated secret path.

**Zero configuration required** - the add-on automatically:
- Discovers your Home Assistant URL
- Authenticates using Supervisor token
- Generates a secure random endpoint path
- Configures secure communication

**Finding your connection URL:**

Check the add-on logs after startup to find your unique MCP server URL:

```
🔐 MCP Server URL: http://192.168.1.100:9583/private_zctpwlX7ZkIAr7oqdfLPxw

   Secret Path: /private_zctpwlX7ZkIAr7oqdfLPxw

   ⚠️  IMPORTANT: Copy this exact URL - the secret path is required!
```

**Client Configuration Example (Claude Desktop):**

```json
{
  "mcpServers": {
    "home-assistant": {
      "url": "http://192.168.1.100:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
      "transport": "http"
    }
  }
}
```

For more AI assistant setup examples, see:
https://github.com/homeassistant-ai/ha-mcp#client-configuration

## Security

### Auto-Generated Secret Paths

The add-on generates a unique 128-bit random path for each installation:

- **High Entropy**: 128 bits of cryptographic randomness using URL-safe encoding
- **Persistent**: Saved to `/data/secret_path.txt` and reused on restarts
- **Defense in Depth**: Prevents unauthorized access even on local networks
- **No Configuration**: Works out of the box with maximum security

### Network Security

- The addon listens on port 9583 (internal container port)
- Port mapping exposes it to your local Home Assistant network only
- Not accessible from the internet unless you explicitly configure port forwarding
- Secret path acts as authentication - only those with the URL can access

### Custom Secret Paths

If you need to set a custom path (for migration or specific requirements):

1. Enable "Advanced" mode in the add-on configuration UI
2. Set the `secret_path` option to your desired path
3. Restart the add-on

## Troubleshooting

### Add-on won't start

Check the add-on logs for errors. Common issues:
- Invalid configuration in config.yaml
- Python dependency installation failures

### AI assistant can't connect

Verify:
1. Add-on is running (check status in Add-ons page)
2. You copied the **complete URL** including the secret path from the logs
3. Your MCP client is configured correctly
4. Check add-on logs for connection attempts

### Lost the secret URL

The secret path is stored in `/data/secret_path.txt`. You can:

1. Check the add-on logs (scroll to startup messages)
2. Access the add-on's file system and read `/data/secret_path.txt`
3. Or restart the add-on to see the URL logged again

### Operations failing

Check add-on logs for detailed error messages. The add-on sanitizes errors to prevent token leakage while maintaining usefulness.

## Support

- **Issues**: https://github.com/homeassistant-ai/ha-mcp/issues
- **Documentation**: https://github.com/homeassistant-ai/ha-mcp
- **Wiki**: https://github.com/homeassistant-ai/ha-mcp/wiki
