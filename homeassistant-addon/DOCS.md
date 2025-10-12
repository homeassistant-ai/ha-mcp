# Home Assistant MCP Server Add-on

AI assistant integration for Home Assistant via Model Context Protocol (MCP).

## About

This add-on enables AI assistants (Claude, ChatGPT, etc.) to control your Home Assistant installation through the Model Context Protocol (MCP). It provides 20+ tools for device control, automation management, entity search, backup/restore, and system queries.

**Key Features:**
- **Zero Configuration** - Automatically discovers Home Assistant connection
- **Secure by Default** - Auto-generated secret paths with 128-bit entropy
- **Fuzzy Search** - Find entities even with typos
- **Backup & Restore** - Safe configuration management

Full features and documentation: https://github.com/homeassistant-ai/ha-mcp

---

## Installation


1. **Click the button to add the repository** to your Home Assistant instance:

   [![Add Repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fhomeassistant-ai%2Fha-mcp)

   Or manually add this repository URL in Supervisor → Add-on Store:
   ```
   https://github.com/homeassistant-ai/ha-mcp
   ```

2. **Navigate to the add-on** "Home Assistant MCP Server" from the add-on store

3. **Click Install, Wait and then Start**

4. **Check the add-on logs** for your unique MCP server URL:

   ```
   🔐 MCP Server URL: http://192.168.1.100:9583/private_zctpwlX7ZkIAr7oqdfLPxw

   ```

5. **Configure your AI client** using one of the options below

---

## Client Configuration

### <details><summary><b>📱 Claude Desktop or any mcp.json format</b></summary>

Add to your Claude Desktop `mcp.json` configuration file:

**Location:**
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

#won't work. no http support in claude. let's think about it.
**Configuration:**
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

Replace the URL with the one from your add-on logs.

**Restart Claude Desktop** after saving the configuration.

</details>

### <details><summary><b>💻 Claude Code</b></summary>

Use the `claude mcp add` command:

```bash
claude mcp add-json home-assistant '{
  "url": "http://192.168.1.100:9583/private_zctpwlX7ZkIAr7oqdfLPxw",
  "transport": "http"
}'
```

Replace the URL with the one from your add-on logs.

**Restart Claude Code** after adding the configuration.

</details>

### <details><summary><b>🌐 Web Clients (Claude.ai, ChatGPT, etc.)</b></summary>

For secure remote access without port forwarding, use the **Cloudflared add-on**:

#### Install Cloudflared Add-on

[![Add Cloudflared Repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fbrenner-tobias%2Faddon-cloudflared)

#### Configure Cloudflared

Add to Cloudflared add-on configuration:

```yaml
additional_hosts:
  - hostname: ha-mcp  # Quick tunnel mode (generates temporary URL)
    service: http://localhost:9583
```

Or with a custom domain:
```yaml
additional_hosts:
  - hostname: ha-mcp.yourdomain.com
    service: http://localhost:9583
```

#### Get Your Public URL

After starting Cloudflared, check its logs for your tunnel URL:
- Quick tunnel: `https://random-name.trycloudflare.com`
- Custom domain: `https://ha-mcp.yourdomain.com`

#### Use Your MCP Server

Combine the Cloudflare tunnel URL with your secret path:
```
https://random-name.trycloudflare.com/private_zctpwlX7ZkIAr7oqdfLPxw
```

**Benefits:**
- No port forwarding required
- Automatic HTTPS encryption
- Optional Cloudflare Zero Trust authentication
- Centrally managed with other Home Assistant services

See [Cloudflared add-on documentation](https://github.com/brenner-tobias/addon-cloudflared/blob/main/cloudflared/DOCS.md) for advanced configuration.

</details>

---

## Configuration Options

The add-on has minimal configuration - most settings are automatic.

### backup_hint (Advanced)

**Default:** `normal`

Controls when the AI assistant suggests creating backups before operations:

- `normal` (recommended): Before irreversible operations only
- `strong`: Before first modification of each session
- `weak`: Rarely suggests backups
- `auto`: Intelligent detection (future enhancement)

**Note:** This is an advanced option. Enable "Show unused optional configuration options" in the add-on configuration UI to see it.

### secret_path (Advanced)

**Default:** Empty (auto-generated)

Custom secret path override. **Leave empty for auto-generation** (recommended).

- When empty, the add-on generates a secure 128-bit random path on first start
- The path is persisted to `/data/secret_path.txt` and reused on restarts
- Custom paths are useful for migration or specific security requirements

**Note:** This is an advanced option. Enable "Show unused optional configuration options" in the add-on configuration UI to see it.

**Example Configuration:**

```yaml
backup_hint: normal
secret_path: ""  # Leave empty for auto-generation
```

---

## Security

### Auto-Generated Secret Paths

The add-on automatically generates a unique secret path on first startup using 128-bit cryptographic entropy. This ensures:

- Each installation has a unique, unpredictable endpoint
- The secret is persisted across restarts
- No manual configuration needed

### Authentication

The add-on uses Home Assistant Supervisor's built-in authentication. No tokens or credentials are needed - the add-on automatically authenticates with your Home Assistant instance.

### Network Exposure

- **Local network only by default** - The add-on listens on port 9583
- **Remote access** - Use the Cloudflared add-on for secure HTTPS tunnels
- **Never expose** port 9583 directly to the internet without proper security measures

---

## Troubleshooting

### Add-on won't start

**Check the logs** for errors:
- Configuration validation errors
- Dependency installation failures
- Port conflicts (9583 already in use)

**Solution:** Review the error message and adjust configuration or free up the port.

### Can't connect to MCP server

**Verify:**
1. Add-on is running (check status in Supervisor)
2. You copied the **complete URL** including the secret path from logs
3. Your MCP client configuration is correct
4. No firewall blocking port 9583 on your local network

**Solution:** Restart the add-on and copy the URL from fresh logs.

### Lost the secret URL

**Options:**
1. Check the add-on logs (scroll to startup messages)
2. Restart the add-on (logs will show the URL again)
3. Read directly from `/data/secret_path.txt` using the Terminal & SSH add-on
4. Generate a new secret by deleting `/data/secret_path.txt` and restarting

### Operations failing

**Check add-on logs** for detailed error messages. Common issues:

- Invalid entity IDs (use fuzzy search to find correct IDs)
- Missing permissions (add-on should have full access)
- Home Assistant API errors (check HA logs)

**Solution:** Review the specific error in logs and adjust your commands accordingly.

### Performance issues

If the add-on is slow or unresponsive:

1. Check Home Assistant system resources (CPU, memory)
2. Review add-on logs for warnings
3. Restart the add-on
4. Consider reducing concurrent AI assistant operations

---

## Available Tools

The add-on provides 20+ MCP tools for controlling Home Assistant:

### Core Tools
- `ha_search_entities` - Fuzzy entity search
- `ha_get_overview` - System overview
- `ha_get_state` - Entity state with details
- `ha_call_service` - Universal service control

### Configuration Management
- `ha_config_set_helper` - Create/update helpers
- `ha_config_remove_helper` - Delete helpers
- `ha_config_set_script` - Create/update scripts
- `ha_config_get_script` - Get script configuration
- `ha_config_remove_script` - Delete scripts
- `ha_config_set_automation` - Create/update automations
- `ha_config_get_automation` - Get automation configuration
- `ha_config_remove_automation` - Delete automations

### Device Control
- `ha_bulk_control` - Multi-device control with verification
- `ha_get_operation_status` - Check operation status
- `ha_get_bulk_status` - Check multiple operations

### Convenience
- `ha_activate_scene` - Activate scenes
- `ha_get_weather` - Weather information
- `ha_get_energy` - Energy usage data
- `ha_get_logbook` - Historical events

### Backup & Restore
- `ha_backup_create` - Fast local backups
- `ha_backup_restore` - Restore from backup

### Advanced
- `ha_eval_template` - Evaluate Jinja2 templates
- `ha_get_domain_docs` - Domain documentation

See the [main repository](https://github.com/homeassistant-ai/ha-mcp) for detailed tool documentation and examples.

---

## Support

**Issues and Bug Reports:**
https://github.com/homeassistant-ai/ha-mcp/issues

**Documentation:**
https://github.com/homeassistant-ai/ha-mcp

**Contributing:**
https://github.com/homeassistant-ai/ha-mcp/blob/master/CONTRIBUTING.md

---

## License

This add-on is licensed under the MIT License.

See [LICENSE](https://github.com/homeassistant-ai/ha-mcp/blob/master/LICENSE) for full license text.
