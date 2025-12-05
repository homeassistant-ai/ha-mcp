#!/bin/bash
# ha-mcp installer for macOS
# Usage: curl -LsSf https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/main/scripts/install-macos.sh | bash
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo ""
echo -e "${BLUE}============================================${NC}"
echo -e "${BLUE}   ha-mcp Installer for macOS${NC}"
echo -e "${BLUE}============================================${NC}"
echo ""

# Configuration
CONFIG_DIR="$HOME/Library/Application Support/Claude"
CONFIG_FILE="$CONFIG_DIR/claude_desktop_config.json"
DEMO_URL="https://ha-mcp-demo-server.qc-h.net"
DEMO_TOKEN="demo"

# Step 1: Check/install uv
echo -e "${YELLOW}Step 1: Checking for uv...${NC}"
if command -v uv &> /dev/null || command -v uvx &> /dev/null; then
    echo -e "${GREEN}  uv is already installed${NC}"
else
    echo -e "  Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Source the new path
    export PATH="$HOME/.local/bin:$PATH"
    if command -v uvx &> /dev/null; then
        echo -e "${GREEN}  uv installed successfully${NC}"
    else
        echo -e "${RED}  Failed to install uv. Please install manually:${NC}"
        echo "  brew install uv"
        echo "  OR"
        echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
        exit 1
    fi
fi
echo ""

# Step 2: Configure Claude Desktop
echo -e "${YELLOW}Step 2: Configuring Claude Desktop...${NC}"
CLAUDE_NOT_INSTALLED=false
if [ ! -d "$CONFIG_DIR" ]; then
    CLAUDE_NOT_INSTALLED=true
    echo -e "  Claude Desktop not yet installed - creating config for later"
fi

# Create config directory if needed
mkdir -p "$CONFIG_DIR"

# The MCP server config to add
HA_MCP_CONFIG='{
  "command": "uvx",
  "args": ["ha-mcp@latest"],
  "env": {
    "HOMEASSISTANT_URL": "'"$DEMO_URL"'",
    "HOMEASSISTANT_TOKEN": "'"$DEMO_TOKEN"'"
  }
}'

# Check if config file exists and handle accordingly
if [ -f "$CONFIG_FILE" ]; then
    # Backup existing config
    BACKUP_FILE="${CONFIG_FILE}.backup.$(date +%Y%m%d_%H%M%S)"
    cp "$CONFIG_FILE" "$BACKUP_FILE"
    echo -e "  Backed up existing config to:"
    echo -e "  ${BLUE}$BACKUP_FILE${NC}"

    # Check if ha-mcp is already configured
    if grep -q '"Home Assistant"' "$CONFIG_FILE" 2>/dev/null; then
        echo -e "${YELLOW}  Home Assistant MCP already configured.${NC}"
        echo "  Updating configuration..."
    fi

    # Use Python to merge the config (available on all Macs)
    python3 << EOF
import json
import sys

config_file = "$CONFIG_FILE"
demo_url = "$DEMO_URL"
demo_token = "$DEMO_TOKEN"

try:
    with open(config_file, 'r') as f:
        content = f.read().strip()
        if content:
            config = json.loads(content)
        else:
            config = {}
except (json.JSONDecodeError, FileNotFoundError):
    config = {}

# Ensure mcpServers exists
if 'mcpServers' not in config:
    config['mcpServers'] = {}

# Add/update Home Assistant config
config['mcpServers']['Home Assistant'] = {
    "command": "uvx",
    "args": ["ha-mcp@latest"],
    "env": {
        "HOMEASSISTANT_URL": demo_url,
        "HOMEASSISTANT_TOKEN": demo_token
    }
}

with open(config_file, 'w') as f:
    json.dump(config, f, indent=2)

print("  Configuration updated successfully")
EOF
else
    # Create new config file
    cat > "$CONFIG_FILE" << EOF
{
  "mcpServers": {
    "Home Assistant": {
      "command": "uvx",
      "args": ["ha-mcp@latest"],
      "env": {
        "HOMEASSISTANT_URL": "$DEMO_URL",
        "HOMEASSISTANT_TOKEN": "$DEMO_TOKEN"
      }
    }
  }
}
EOF
    echo -e "  Created new configuration file"
fi
echo -e "${GREEN}  Claude Desktop configured${NC}"
echo ""

# Success message
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}   Installation Complete!${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""
echo -e "${YELLOW}Next steps:${NC}"
echo ""
if [ "$CLAUDE_NOT_INSTALLED" = true ]; then
    echo -e "  1. Download Claude Desktop: ${BLUE}https://claude.ai/download${NC}"
    echo "  2. Create a free account at claude.ai (if you haven't)"
    echo "  3. Open Claude Desktop and ask: \"Can you see my Home Assistant?\""
else
    echo "  1. Quit Claude Desktop: Claude menu > Quit Claude"
    echo "  2. Reopen and ask: \"Can you see my Home Assistant?\""
fi
echo ""
echo -e "${YELLOW}Note:${NC} If Claude Desktop was already running, you must restart it"
echo "      to load the new configuration."
echo ""
echo -e "${BLUE}Demo environment:${NC}"
echo "  Web UI: $DEMO_URL"
echo "  Login:  mcp / mcp"
echo "  (Resets weekly - changes won't persist)"
echo ""
echo -e "${YELLOW}To use YOUR Home Assistant:${NC}"
echo "  Edit: $CONFIG_FILE"
echo "  Replace HOMEASSISTANT_URL with your HA URL"
echo "  Replace HOMEASSISTANT_TOKEN with your token"
echo "  (Generate token in HA: Profile > Security > Long-lived tokens)"
echo ""
