#!/bin/sh
# ha-mcp installer for macOS and Linux
#
# Default (Claude Desktop):
#   curl -LsSf https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/master/scripts/install.sh | sh
#
# Claude Code (official CLI — requires a paid Claude plan):
#   curl -LsSf https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/master/scripts/install.sh | sh -s -- --claude-code
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Which client to configure: "desktop" (default) or "claude-code"
CLIENT="desktop"
for arg in "$@"; do
    case "$arg" in
        --claude-code) CLIENT="claude-code" ;;
        --desktop)     CLIENT="desktop" ;;
        -h|--help)
            printf "Usage: install.sh [--claude-code | --desktop]\n"
            printf "  --desktop      Configure Claude Desktop (default)\n"
            printf "  --claude-code  Configure Claude Code (CLI — requires a paid Claude plan)\n"
            exit 0
            ;;
        *) printf "${YELLOW}Ignoring unknown option: %s${NC}\n" "$arg" >&2 ;;
    esac
done

# Detect OS and set platform-specific values
OS=$(uname -s)
case "$OS" in
    Darwin)
        PLATFORM="macOS"
        CONFIG_DIR="$HOME/Library/Application Support/Claude"
        CLAUDE_DESKTOP_STEP="  1. Download Claude Desktop: ${BLUE}https://claude.ai/download${NC}\n"
        RESTART_STEP="  1. Quit Claude Desktop: Claude menu > Quit Claude"
        ;;
    Linux)
        PLATFORM="Linux"
        CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/Claude"
        CLAUDE_DESKTOP_STEP="  1. Install Claude Desktop for Linux (community build): ${BLUE}https://github.com/aaddrick/claude-desktop-debian${NC}\n     (Unofficial — Anthropic doesn't ship Claude Desktop for Linux.)\n"
        RESTART_STEP="  1. Quit Claude Desktop and reopen it"
        ;;
    *)
        printf "${RED}Unsupported OS: %s${NC}\n" "$OS"
        printf "See https://github.com/homeassistant-ai/ha-mcp for manual setup.\n"
        exit 1
        ;;
esac

CONFIG_FILE="$CONFIG_DIR/claude_desktop_config.json"
DEMO_URL="https://ha-mcp-demo-server.qc-h.net"
DEMO_TOKEN="demo"

# Detect WSL — Claude Desktop runs on the Windows host and reads %APPDATA%\Claude,
# so writing the Linux config does nothing. Claude Code, by contrast, runs fine in WSL.
IS_WSL=false
if [ "$OS" = "Linux" ]; then
    if [ -n "${WSL_DISTRO_NAME:-}" ] || grep -qiE 'microsoft|wsl' /proc/sys/kernel/osrelease 2>/dev/null; then
        IS_WSL=true
    fi
fi
if [ "$IS_WSL" = true ] && [ "$CLIENT" = "desktop" ]; then
    printf "${RED}Detected WSL (Windows Subsystem for Linux).${NC}\n" >&2
    printf "Claude Desktop runs on Windows and reads its config from %%APPDATA%%\\\\Claude\\\\,\n" >&2
    printf "not the WSL filesystem — configuring it here would do nothing.\n\n" >&2
    printf "Run the Windows installer from PowerShell instead:\n" >&2
    printf "  ${BLUE}irm https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/master/scripts/install-windows.ps1 | iex${NC}\n\n" >&2
    printf "Or use Claude Code inside WSL (supported) by re-running with --claude-code:\n" >&2
    printf "  ${BLUE}curl -LsSf https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/master/scripts/install.sh | sh -s -- --claude-code${NC}\n" >&2
    exit 1
fi

printf "\n"
printf "${BLUE}============================================${NC}\n"
if [ "$CLIENT" = "claude-code" ]; then
    printf "${BLUE}   ha-mcp Installer for %s (Claude Code)${NC}\n" "$PLATFORM"
else
    printf "${BLUE}   ha-mcp Installer for %s${NC}\n" "$PLATFORM"
fi
printf "${BLUE}============================================${NC}\n"
printf "\n"

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
        case "$OS" in
            Darwin)
                printf "  brew install uv\n"
                printf "  OR\n"
                ;;
        esac
        printf "  curl -LsSf https://astral.sh/uv/install.sh | sh\n"
        exit 1
    fi
fi

# Get full path to uvx (Claude Desktop and Claude Code don't inherit shell PATH)
UVX_PATH=""
if command -v uvx > /dev/null 2>&1; then
    UVX_PATH=$(command -v uvx)
elif [ -x "$HOME/.local/bin/uvx" ]; then
    UVX_PATH="$HOME/.local/bin/uvx"
elif [ -x "/usr/local/bin/uvx" ]; then
    UVX_PATH="/usr/local/bin/uvx"
elif [ "$OS" = "Darwin" ] && [ -x "/opt/homebrew/bin/uvx" ]; then
    UVX_PATH="/opt/homebrew/bin/uvx"
else
    printf "${RED}  Could not find uvx. Please check your installation.${NC}\n"
    exit 1
fi
printf "  Using uvx at: ${BLUE}%s${NC}\n" "$UVX_PATH"
printf "\n"

# ----------------------------------------------------------------------------
# Claude Code path (opt-in via --claude-code)
# ----------------------------------------------------------------------------
if [ "$CLIENT" = "claude-code" ]; then
    printf "${YELLOW}Step 2: Configuring Claude Code...${NC}\n"

    if ! command -v claude > /dev/null 2>&1 && [ ! -x "$HOME/.local/bin/claude" ]; then
        printf "  Claude Code not found - installing...\n"
        CC_INSTALLER=$(curl -fsSL https://claude.ai/install.sh) || {
            printf "${RED}  Failed to download the Claude Code installer.${NC}\n" >&2
            printf "  Install it manually, then re-run this script:\n" >&2
            printf "    curl -fsSL https://claude.ai/install.sh | bash\n" >&2
            exit 1
        }
        [ -n "$CC_INSTALLER" ] || {
            printf "${RED}  Claude Code installer download was empty.${NC}\n" >&2
            exit 1
        }
        # The official installer is a bash script; fall back to sh if bash is absent.
        INSTALL_SHELL=$(command -v bash || command -v sh)
        if [ -z "$INSTALL_SHELL" ]; then
            printf "${RED}  No bash or sh found to run the Claude Code installer.${NC}\n" >&2
            exit 1
        fi
        printf '%s\n' "$CC_INSTALLER" | "$INSTALL_SHELL" || {
            printf "${RED}  Claude Code installation failed.${NC}\n" >&2
            printf "  See https://code.claude.com/docs/en/setup for manual steps.\n" >&2
            exit 1
        }
        export PATH="$HOME/.local/bin:$PATH"
    else
        printf "${GREEN}  Claude Code is already installed${NC}\n"
    fi

    # Resolve the claude binary
    CLAUDE_BIN=""
    if command -v claude > /dev/null 2>&1; then
        CLAUDE_BIN=$(command -v claude)
    elif [ -x "$HOME/.local/bin/claude" ]; then
        CLAUDE_BIN="$HOME/.local/bin/claude"
    fi
    if [ -z "$CLAUDE_BIN" ]; then
        printf "${RED}  Could not find the 'claude' command after install.${NC}\n" >&2
        printf "  Open a new terminal (so PATH picks up ~/.local/bin) and re-run with --claude-code.\n" >&2
        exit 1
    fi

    # Configure ha-mcp at user scope so it's available in every project.
    # Remove any prior entry first so re-running this script is idempotent.
    "$CLAUDE_BIN" mcp remove home-assistant --scope user > /dev/null 2>&1 || true
    if "$CLAUDE_BIN" mcp add home-assistant --scope user \
        --env HOMEASSISTANT_URL="$DEMO_URL" \
        --env HOMEASSISTANT_TOKEN="$DEMO_TOKEN" \
        -- "$UVX_PATH" --python 3.13 --refresh ha-mcp@latest; then
        printf "${GREEN}  Claude Code configured (server: home-assistant)${NC}\n"
    else
        printf "${RED}  'claude mcp add' failed.${NC}\n" >&2
        printf "  Configure it manually:\n" >&2
        printf "    claude mcp add home-assistant --scope user \\\\\n" >&2
        printf "      --env HOMEASSISTANT_URL=%s \\\\\n" "$DEMO_URL" >&2
        printf "      --env HOMEASSISTANT_TOKEN=%s \\\\\n" "$DEMO_TOKEN" >&2
        printf "      -- %s --python 3.13 --refresh ha-mcp@latest\n" "$UVX_PATH" >&2
        exit 1
    fi
    printf "\n"

    # Step 3: Pre-download dependencies
    printf "${YELLOW}Step 3: Pre-downloading ha-mcp...${NC}\n"
    printf "  This speeds up the first Claude Code session...\n"
    if "$UVX_PATH" --python 3.13 --refresh ha-mcp@latest --version > /dev/null 2>&1; then
        printf "${GREEN}  Dependencies cached${NC}\n"
    else
        printf "${YELLOW}  Could not pre-download ha-mcp (network or PyPI issue).${NC}\n"
        printf "${YELLOW}  Not fatal — it will download on first use. To retry now:${NC}\n"
        printf "    %s --python 3.13 --refresh ha-mcp@latest --version\n" "$UVX_PATH"
    fi
    printf "\n"

    printf "${GREEN}============================================${NC}\n"
    printf "${GREEN}   Installation Complete!${NC}\n"
    printf "${GREEN}============================================${NC}\n"
    printf "\n"
    printf "${YELLOW}Next steps:${NC}\n"
    printf "\n"
    printf "  1. Start Claude Code:  ${BLUE}claude${NC}\n"
    printf "     (Claude Code requires a paid plan — Pro, Max, Team, or Enterprise.)\n"
    printf "  2. Run ${BLUE}/mcp${NC} inside Claude Code to confirm \"home-assistant\" is connected\n"
    printf "  3. Ask: \"Can you see my Home Assistant?\"\n"
    printf "\n"
    printf "${BLUE}Demo environment:${NC}\n"
    printf "  Web UI: %s\n" "$DEMO_URL"
    printf "  Login:  mcp / mcp\n"
    printf "  (Resets weekly - changes won't persist)\n"
    printf "\n"
    printf "${YELLOW}To use YOUR Home Assistant:${NC}\n"
    printf "  claude mcp remove home-assistant --scope user\n"
    printf "  then re-add with your own HOMEASSISTANT_URL / HOMEASSISTANT_TOKEN\n"
    printf "  (Generate a token in HA: Profile > Security > Long-lived tokens)\n"
    printf "\n"
    exit 0
fi

# ----------------------------------------------------------------------------
# Claude Desktop path (default)
# ----------------------------------------------------------------------------
# Step 2: Configure Claude Desktop
printf "${YELLOW}Step 2: Configuring Claude Desktop...${NC}\n"
CLAUDE_NOT_INSTALLED=false
if [ ! -d "$CONFIG_DIR" ]; then
    CLAUDE_NOT_INSTALLED=true
    printf "  Claude Desktop not yet installed - creating config for later\n"
fi

# Create config directory if needed
mkdir -p "$CONFIG_DIR"

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

    # Merging an existing config needs python3
    if ! command -v python3 > /dev/null 2>&1; then
        printf "${RED}  python3 is required to merge your existing Claude config, but it isn't installed.${NC}\n" >&2
        printf "  Your existing config is untouched (backup above). Install python3 and re-run:\n" >&2
        printf "    Debian/Ubuntu:  sudo apt install python3\n" >&2
        printf "    Fedora/RHEL:    sudo dnf install python3\n" >&2
        printf "    Alpine:         sudo apk add python3\n" >&2
        printf "    macOS:          brew install python3\n" >&2
        exit 1
    fi

    # Use Python to merge the config. Values are passed via the environment and the
    # heredoc delimiter is quoted ('EOF'), so paths with quotes/backslashes/newlines
    # can't break the Python source or inject code.
    HA_CONFIG_FILE="$CONFIG_FILE" HA_BACKUP_FILE="$BACKUP_FILE" \
    HA_DEMO_URL="$DEMO_URL" HA_DEMO_TOKEN="$DEMO_TOKEN" HA_UVX_PATH="$UVX_PATH" \
    python3 << 'EOF'
import json
import os
import sys
import tempfile

config_file = os.environ["HA_CONFIG_FILE"]
backup_file = os.environ["HA_BACKUP_FILE"]
demo_url = os.environ["HA_DEMO_URL"]
demo_token = os.environ["HA_DEMO_TOKEN"]
uvx_path = os.environ["HA_UVX_PATH"]

try:
    with open(config_file, 'r') as f:
        content = f.read().strip()
        config = json.loads(content) if content else {}
except FileNotFoundError:
    config = {}
except json.JSONDecodeError as e:
    sys.stderr.write(
        "  WARNING: existing config is not valid JSON (%s).\n"
        "  Starting from a fresh config. Your original file — including any other\n"
        "  MCP servers it defined — is preserved in the backup:\n"
        "    %s\n"
        "  Merge those servers back manually if you need them.\n" % (e, backup_file)
    )
    config = {}

if 'mcpServers' not in config:
    config['mcpServers'] = {}

# Add/update Home Assistant config (using full path for Claude Desktop compatibility)
config['mcpServers']['Home Assistant'] = {
    "command": uvx_path,
    "args": ["--python", "3.13", "--refresh", "ha-mcp@latest"],
    "env": {
        "HOMEASSISTANT_URL": demo_url,
        "HOMEASSISTANT_TOKEN": demo_token
    }
}

# Write atomically: a crash mid-write must not truncate the user's config.
dir_name = os.path.dirname(config_file) or "."
fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
try:
    with os.fdopen(fd, 'w') as f:
        json.dump(config, f, indent=2)
    os.replace(tmp_path, config_file)
except Exception:
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    raise

print("  Configuration updated successfully")
EOF
else
    # Create new config file (using full path for Claude Desktop compatibility).
    # Write to a private temp file then move into place so a crash can't leave a
    # partial config, and so the token is never briefly world-readable.
    TMP_CONFIG="${CONFIG_FILE}.tmp"
    trap 'rm -f "$TMP_CONFIG"' EXIT INT TERM
    if ! cat > "$TMP_CONFIG" << EOF
{
  "mcpServers": {
    "Home Assistant": {
      "command": "$UVX_PATH",
      "args": ["--python", "3.13", "--refresh", "ha-mcp@latest"],
      "env": {
        "HOMEASSISTANT_URL": "$DEMO_URL",
        "HOMEASSISTANT_TOKEN": "$DEMO_TOKEN"
      }
    }
  }
}
EOF
    then
        printf "${RED}  Failed to write config (disk full or permission denied?).${NC}\n" >&2
        exit 1
    fi
    chmod 600 "$TMP_CONFIG" 2>/dev/null || true
    if ! mv "$TMP_CONFIG" "$CONFIG_FILE"; then
        printf "${RED}  Failed to move config into place: %s${NC}\n" "$CONFIG_FILE" >&2
        exit 1
    fi
    trap - EXIT INT TERM
    printf "  Created new configuration file\n"
fi
printf "${GREEN}  Claude Desktop configured${NC}\n"
printf "\n"

# Step 3: Pre-download dependencies
printf "${YELLOW}Step 3: Pre-downloading ha-mcp...${NC}\n"
printf "  This speeds up Claude Desktop startup...\n"
if "$UVX_PATH" --python 3.13 --refresh ha-mcp@latest --version > /dev/null 2>&1; then
    printf "${GREEN}  Dependencies cached${NC}\n"
else
    printf "${YELLOW}  Could not pre-download ha-mcp (network or PyPI issue).${NC}\n"
    printf "${YELLOW}  Not fatal — Claude Desktop will download it on first launch,${NC}\n"
    printf "${YELLOW}  which only makes the first start slower. To retry now:${NC}\n"
    printf "    %s --python 3.13 --refresh ha-mcp@latest --version\n" "$UVX_PATH"
fi
printf "\n"

# Success message
printf "${GREEN}============================================${NC}\n"
printf "${GREEN}   Installation Complete!${NC}\n"
printf "${GREEN}============================================${NC}\n"
printf "\n"
printf "${YELLOW}Next steps:${NC}\n"
printf "\n"
if [ "$CLAUDE_NOT_INSTALLED" = true ]; then
    printf "%b" "$CLAUDE_DESKTOP_STEP"
    printf "  2. Create a free account at claude.ai (if you haven't)\n"
    printf "  3. Open Claude Desktop and ask: \"Can you see my Home Assistant?\"\n"
else
    printf "%s\n" "$RESTART_STEP"
    printf "  2. Ask: \"Can you see my Home Assistant?\"\n"
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
if [ "$OS" = "Linux" ]; then
    printf "${BLUE}Prefer the official CLI?${NC} Claude Code also works on Linux (paid plan):\n"
    printf "  curl -LsSf https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/master/scripts/install.sh | sh -s -- --claude-code\n"
    printf "\n"
fi
