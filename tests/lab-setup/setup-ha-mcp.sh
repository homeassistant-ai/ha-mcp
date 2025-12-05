#!/usr/bin/env bash
set -euo pipefail

#=============================================================================
# ha-mcp Test Environment Setup Script
# Usage: sudo ./setup-ha-mcp.sh [domain]
# Idempotent - safe to re-run
#=============================================================================

DOMAIN="${1:-ha-mcp-demo-server.qc-h.net}"
SWAP_SIZE_GB=6
HA_PORT=8123
SETUP_USER="${SUDO_USER:-$USER}"
SETUP_HOME=$(eval echo "~$SETUP_USER")
UV_PATH="$SETUP_HOME/.local/bin/uv"

#=============================================================================
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

#=============================================================================
[[ $EUID -ne 0 ]] && error "Run as root: sudo $0 [domain]"
info "Setting up ha-mcp test env for user: $SETUP_USER"
info "Domain: $DOMAIN"

#=============================================================================
# 1. SWAP
if [[ $SWAP_SIZE_GB -gt 0 ]]; then
    if [[ ! -f /swapfile ]]; then
        info "Creating ${SWAP_SIZE_GB}GB swap..."
        fallocate -l ${SWAP_SIZE_GB}G /swapfile
        chmod 600 /swapfile
        mkswap /swapfile
        swapon /swapfile
        grep -q "^/swapfile" /etc/fstab || echo "/swapfile none swap sw 0 0" >> /etc/fstab
    else
        swapon /swapfile 2>/dev/null || true
        info "Swap already configured"
    fi
fi

#=============================================================================
# 2. PACKAGES
info "Installing packages..."
apt-get update -qq
apt-get install -y -qq curl git ca-certificates gnupg

#=============================================================================
# 3. DOCKER
if ! command -v docker &>/dev/null; then
    info "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
else
    info "Docker already installed"
fi

if ! id -nG "$SETUP_USER" | grep -qw docker; then
    info "Adding $SETUP_USER to docker group..."
    usermod -aG docker "$SETUP_USER"
fi

#=============================================================================
# 4. UV
if [[ ! -f "$UV_PATH" ]]; then
    info "Installing uv..."
    sudo -u "$SETUP_USER" bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
else
    info "uv already installed"
fi

#=============================================================================
# 5. HA-MCP REPO
if [[ ! -d "$SETUP_HOME/ha-mcp" ]]; then
    info "Cloning ha-mcp..."
    sudo -u "$SETUP_USER" git clone https://github.com/homeassistant-ai/ha-mcp "$SETUP_HOME/ha-mcp"
else
    info "ha-mcp repo exists, pulling latest..."
    sudo -u "$SETUP_USER" git -C "$SETUP_HOME/ha-mcp" pull --ff-only || true
fi

#=============================================================================
# 6. CRONTAB
info "Setting up crontab..."
CRON_CMD="@reboot sleep 10 && cd $SETUP_HOME/ha-mcp && HA_TEST_PORT=$HA_PORT $UV_PATH run hamcp-test-env --no-interactive >> /tmp/hamcp.log 2>&1"
( sudo -u "$SETUP_USER" crontab -l 2>/dev/null | grep -v "hamcp-test-env" || true; echo "$CRON_CMD" ) | sudo -u "$SETUP_USER" crontab -

#=============================================================================
# 7. CADDY
if [[ -n "$DOMAIN" ]]; then
    if ! command -v caddy &>/dev/null; then
        info "Installing Caddy..."
        apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' 2>/dev/null | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' 2>/dev/null | tee /etc/apt/sources.list.d/caddy-stable.list > /dev/null
        apt-get update -qq
        apt-get install -y -qq caddy
    else
        info "Caddy already installed"
    fi

    info "Configuring Caddy for $DOMAIN..."
    tee /etc/caddy/Caddyfile > /dev/null << CADDYEOF
${DOMAIN} {
    reverse_proxy 127.0.0.1:${HA_PORT} {
        transport http {
            read_timeout 300s
            write_timeout 300s
        }
    }
}
CADDYEOF
    systemctl enable caddy
    systemctl reload caddy || systemctl restart caddy
fi

#=============================================================================
# 8. STOP OLD CONTAINERS
info "Cleaning up old containers..."
docker ps -aq --filter "ancestor=ghcr.io/home-assistant/home-assistant" | xargs -r docker rm -f 2>/dev/null || true

#=============================================================================
# 9. START HA-MCP
info "Starting hamcp-test-env..."
sudo -u "$SETUP_USER" sg docker -c "cd $SETUP_HOME/ha-mcp && HA_TEST_PORT=$HA_PORT $UV_PATH run hamcp-test-env --no-interactive > /tmp/hamcp.log 2>&1 &"

#=============================================================================
# 10. WAIT FOR HA
info "Waiting for Home Assistant to start..."
for i in {1..60}; do
    if curl -s -o /dev/null -w "%{http_code}" "http://localhost:$HA_PORT" 2>/dev/null | grep -qE "200|401"; then
        break
    fi
    echo -n "."
    sleep 2
done
echo ""

#=============================================================================
# 11. VERIFY
sleep 3
if docker ps | grep -q "home-assistant"; then
    CONTAINER=$(docker ps --filter "ancestor=ghcr.io/home-assistant/home-assistant" --format "{{.Names}}" | head -1)
    echo ""
    echo "=============================================="
    echo -e "${GREEN}Setup Complete!${NC}"
    echo "=============================================="
    echo "Local:       http://localhost:${HA_PORT}"
    [[ -n "$DOMAIN" ]] && echo "External:    https://${DOMAIN}"
    echo "Container:   $CONTAINER"
    echo ""
    echo "Credentials: dev / dev"
    echo ""
    echo "Logs:        docker logs -f $CONTAINER"
    echo "Startup log: tail -f /tmp/hamcp.log"
    echo "=============================================="
else
    echo ""
    echo -e "${RED}Container not running!${NC}"
    echo "Check logs: cat /tmp/hamcp.log"
    exit 1
fi
