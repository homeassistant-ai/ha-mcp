#!/bin/sh
# ha-mcp installer for macOS
# Usage: curl -LsSf https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/main/scripts/install-macos.sh | sh
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

printf "\n"
printf "${BLUE}============================================${NC}\n"
printf "${BLUE}   ha-mcp Installer for macOS${NC}\n"
printf "${BLUE}============================================${NC}\n"
printf "\n"

# Configuration
CONFIG_DIR="$HOME/Library/Application Support/Claude"
CONFIG_FILE="$CONFIG_DIR/claude_desktop_config.json"
DEMO_URL="https://ha-mcp-demo-server.qc-h.net"
DEMO_TOKEN="demo"

# Step 1: Check/install uv
printf "${YELLOW}Step 1: Checking for uv...${NC}\n"
if command -v uv > /dev/null 2>&1 || command -v uvx > /dev/null 2>&1; then
    printf "${GREEN}  uv is already installed${NC}\n"
else
    printf "  Installing uv...\n"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Source the new path
    export PATH="$HOME/.local/bin:$PATH"
    if command -v uvx > /dev/null 2>&1; then
        printf "${GREEN}  uv installed successfully${NC}\n"
    else
        printf "${RED}  Failed to install uv. Please install manually:${NC}\n"
        printf "  brew install uv\n"
        printf "  OR\n"
        printf "  curl -LsSf https://astral.sh/uv/install.sh | sh\n"
        exit 1
    fi
fi
printf "\n"

# Step 2: Configure Claude Desktop
printf "${YELLOW}Step 2: Configuring Claude Desktop...${NC}\n"
CLAUDE_NOT_INSTALLED=false
if [ ! -d "$CONFIG_DIR" ]; then
    CLAUDE_NOT_INSTALLED=true
    printf "  Claude Desktop not yet installed - creating config for later\n"
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
    printf "  Backed up existing config to:\n"
    printf "  ${BLUE}%s${NC}\n" "$BACKUP_FILE"

    # Check if ha-mcp is already configured
    if grep -q '"Home Assistant"' "$CONFIG_FILE" 2>/dev/null; then
        printf "${YELLOW}  Home Assistant MCP already configured.${NC}\n"
        printf "  Updating configuration...\n"
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
    printf "  Created new configuration file\n"
fi
printf "${GREEN}  Claude Desktop configured${NC}\n"
printf "\n"

# Step 3: Pre-download dependencies
printf "${YELLOW}Step 3: Pre-downloading ha-mcp...${NC}\n"
printf "  This speeds up Claude Desktop startup...\n"
uvx ha-mcp@latest --help > /dev/null 2>&1 || true
printf "${GREEN}  Dependencies cached${NC}\n"
printf "\n"

# Success message
printf "${GREEN}============================================${NC}\n"
printf "${GREEN}   Installation Complete!${NC}\n"
printf "${GREEN}============================================${NC}\n"
printf "\n"
printf "${YELLOW}Next steps:${NC}\n"
printf "\n"
if [ "$CLAUDE_NOT_INSTALLED" = true ]; then
    printf "  1. Download Claude Desktop: ${BLUE}https://claude.ai/download${NC}\n"
    printf "  2. Create a free account at claude.ai (if you haven't)\n"
    printf "  3. Open Claude Desktop and ask: \"Can you see my Home Assistant?\"\n"
else
    printf "  1. Quit Claude Desktop: Claude menu > Quit Claude\n"
    printf "  2. Reopen and ask: \"Can you see my Home Assistant?\"\n"
fi
printf "\n"
printf "${YELLOW}Note:${NC} If Claude Desktop was already running, you must restart it\n"
printf "      to load the new configuration.\n"
printf "\n"
printf "${BLUE}Demo environment:${NC}\n"
printf "  Web UI: %s\n" "$DEMO_URL"
printf "  Login:  mcp / mcp\n"
printf "  (Resets weekly - changes won't persist)\n"
printf "\n"
printf "${YELLOW}To use YOUR Home Assistant:${NC}\n"
printf "  Edit: %s\n" "$CONFIG_FILE"
printf "  Replace HOMEASSISTANT_URL with your HA URL\n"
printf "  Replace HOMEASSISTANT_TOKEN with your token\n"
printf "  (Generate token in HA: Profile > Security > Long-lived tokens)\n"
printf "\n"
