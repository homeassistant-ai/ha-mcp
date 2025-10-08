#!/usr/bin/with-contenv bashio

# Home Assistant MCP Server Add-on Start Script
# This script automatically discovers Home Assistant connection details

bashio::log.info "Starting Home Assistant MCP Server..."

# Get configuration options from add-on config
BACKUP_HINT=$(bashio::config 'backup_hint' 'normal')
bashio::log.info "Backup hint mode: ${BACKUP_HINT}"

# Auto-discover Home Assistant URL and token
# The Supervisor provides these automatically via environment variables

# Use the Supervisor API proxy for Home Assistant access
# This is the recommended way to communicate with HA from an add-on
export HOMEASSISTANT_URL="http://supervisor/core"

# The SUPERVISOR_TOKEN environment variable is automatically provided
# and can be used to authenticate with both Supervisor and HA APIs
if [ -z "${SUPERVISOR_TOKEN}" ]; then
    bashio::log.error "SUPERVISOR_TOKEN not found! Cannot authenticate with Home Assistant."
    exit 1
fi

export HOMEASSISTANT_TOKEN="${SUPERVISOR_TOKEN}"
export BACKUP_HINT="${BACKUP_HINT}"

bashio::log.info "Home Assistant URL: ${HOMEASSISTANT_URL}"
bashio::log.info "Authentication configured via Supervisor token"

# Start the MCP server
bashio::log.info "Launching ha-mcp..."
exec ha-mcp
