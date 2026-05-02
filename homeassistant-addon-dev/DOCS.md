# Home Assistant MCP Server (Dev Channel) - Documentation

**WARNING: This is the development channel. Expect bugs and breaking changes.**

This add-on receives updates with every commit to master. For stable releases, use the main "Home Assistant MCP Server" add-on.

## Configuration

The dev add-on uses the same configuration as the stable version. See the main add-on's documentation for full details.

### Options

| Option | Description | Default |
|--------|-------------|---------|
| `backup_hint` | Backup strength preference | `normal` |
| `secret_path` | Custom secret path (optional) | auto-generated |
| `enable_yaml_config_editing` *(beta)* | Enables `ha_config_set_yaml` for editing `configuration.yaml` directly. Requires `ha_mcp_tools` custom component. | `false` |
| `enable_filesystem_tools` *(beta)* | Enables file read/write tools (`ha_list_files`, `ha_read_file`, `ha_write_file`, `ha_delete_file`). Requires `ha_mcp_tools` custom component. | `false` |
| `enable_custom_component_integration` *(beta)* | Enables `ha_install_mcp_tools` installer tool for the `ha_mcp_tools` custom component. | `false` |

Beta options are hidden under "Show unused optional configuration options" in the add-on Configuration tab. See [beta.md](https://github.com/homeassistant-ai/ha-mcp/blob/master/docs/beta.md) for details.

## Updates

The dev channel updates automatically with every commit to master. You may receive multiple updates per day.

To check for updates:
1. Go to Settings > Add-ons
2. Click on "Home Assistant MCP Server (Dev)"
3. Click "Check for updates"

## Switching to Stable

If you want to switch back to stable releases:
1. Uninstall this dev add-on
2. Install the main "Home Assistant MCP Server" add-on

Your configuration will need to be reconfigured.

## Reporting Issues

When reporting issues from the dev channel, please include:
- The commit SHA (shown in the add-on info)
- Steps to reproduce
- Any error logs from the add-on

Issues: https://github.com/homeassistant-ai/ha-mcp/issues
