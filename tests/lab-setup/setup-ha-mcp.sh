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
[[ "$SETUP_USER" == "root" ]] && error "Do not run as root directly. Use: sudo $0 [domain]\nIf calling from a root cron job, set: SUDO_USER=youruser $0 [domain]"
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
# 6. SYSTEMD SERVICE (replaces cron - ensures only one instance runs, auto-restarts)
info "Setting up systemd service..."

# Remove old cron entries if they exist (migration from cron-based setup)
sudo -u "$SETUP_USER" crontab -l 2>/dev/null | grep -v "hamcp-test-env" | sudo -u "$SETUP_USER" crontab - 2>/dev/null || true

cat > /etc/systemd/system/hamcp-demo.service << SVCEOF
[Unit]
Description=HA-MCP Demo Test Environment
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=simple
User=${SETUP_USER}
Group=${SETUP_USER}
WorkingDirectory=${SETUP_HOME}/ha-mcp
Environment=HA_TEST_PORT=${HA_PORT}
ExecStart=${UV_PATH} run hamcp-test-env --no-interactive
Restart=on-failure
RestartSec=30s
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
SVCEOF

cat > /etc/systemd/system/hamcp-demo-update.service << SVCEOF
[Unit]
Description=HA-MCP Demo Weekly Update
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/sudo -i -u ${SETUP_USER} /usr/bin/git -C ${SETUP_HOME}/ha-mcp pull --ff-only
ExecStart=/usr/bin/docker image prune -af
ExecStartPost=/usr/bin/systemctl restart hamcp-demo
SVCEOF

cat > /etc/systemd/system/hamcp-demo-update.timer << SVCEOF
[Unit]
Description=HA-MCP Demo Weekly Update Timer

[Timer]
OnCalendar=Mon *-*-* 03:00:00
AccuracySec=1h
Persistent=true

[Install]
WantedBy=timers.target
SVCEOF

# Remove sudoers rule if it exists from a previous install (no longer needed)
rm -f /etc/sudoers.d/hamcp-demo

systemctl daemon-reload
systemctl enable hamcp-demo.service
systemctl enable hamcp-demo-update.timer
systemctl start hamcp-demo-update.timer

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
# 8. UNATTENDED UPGRADES (auto-updates)
info "Configuring unattended upgrades..."
apt-get install -y -qq unattended-upgrades
cat > /etc/apt/apt.conf.d/50unattended-upgrades << 'UPGRADEEOF'
Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}";
    "${distro_id}:${distro_codename}-security";
    "${distro_id}:${distro_codename}-updates";
};
Unattended-Upgrade::AutoFixInterruptedDpkg "true";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:00";
UPGRADEEOF

cat > /etc/apt/apt.conf.d/20auto-upgrades << 'AUTOEOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::Download-Upgradeable-Packages "1";
APT::Periodic::AutocleanInterval "7";
AUTOEOF

#=============================================================================
# 9. STOP OLD CONTAINERS + PROCESSES
info "Cleaning up old containers and processes..."
docker ps -aq --filter "ancestor=ghcr.io/home-assistant/home-assistant" | xargs -r docker rm -f 2>/dev/null || true
docker ps -aq --filter "ancestor=testcontainers/ryuk" | xargs -r docker rm -f 2>/dev/null || true
pkill -u "$SETUP_USER" -f "hamcp-test-env" 2>/dev/null || true

#=============================================================================
# 10. START HA-MCP VIA SYSTEMD
info "Starting hamcp-demo service..."
systemctl restart hamcp-demo.service

#=============================================================================
# 11. WAIT FOR HA
# On first install, the HA image (~600MB) must be pulled before the container
# starts. Allow up to 15 minutes to cover both pull and boot time.
info "Waiting for Home Assistant to start (up to 15 minutes on first install)..."
HA_READY=0
for i in {1..180}; do
    if curl -s -o /dev/null -w "%{http_code}" "http://localhost:$HA_PORT" 2>/dev/null | grep -qE "200|401"; then
        HA_READY=1
        break
    fi
    echo -n "."
    sleep 5
done
echo ""

#=============================================================================
# 12. VERIFY
if [[ $HA_READY -eq 1 ]]; then
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
    echo "Service log: journalctl -u hamcp-demo -f"
    echo "=============================================="
else
    echo ""
    warn "Home Assistant did not respond within 15 minutes."
    warn "The service is running — HA may still be starting (check: journalctl -u hamcp-demo -f)"
    warn "If it stays down: journalctl -u hamcp-demo --no-pager -n 50"
fi
