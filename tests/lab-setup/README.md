# ha-mcp Lab Setup

This directory contains scripts for setting up a persistent Home Assistant test environment, useful for demos, development, and integration testing.

## Quick Start

```bash
# Download and run on a fresh Ubuntu/Debian server
curl -fsSL https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/master/tests/lab-setup/setup-ha-mcp.sh -o setup-ha-mcp.sh
chmod +x setup-ha-mcp.sh
sudo ./setup-ha-mcp.sh your-domain.example.com
```

## What It Does

The setup script is **idempotent** (safe to re-run) and performs:

1. **Swap Configuration** - Creates 6GB swap for small VMs
2. **Package Installation** - curl, git, ca-certificates, gnupg
3. **Docker Installation** - Via official get.docker.com script
4. **uv Installation** - Python package manager for running ha-mcp
5. **ha-mcp Clone** - Clones the repository to `~/ha-mcp`
6. **Crontab Setup** - Auto-starts hamcp-test-env on reboot
7. **Caddy Reverse Proxy** - HTTPS with automatic Let's Encrypt certificates
8. **Container Cleanup** - Removes old HA containers
9. **Start Test Environment** - Launches hamcp-test-env in background

## Requirements

- Ubuntu/Debian Linux server
- Root access (sudo)
- Domain pointing to server (for HTTPS)
- Ports 80/443 open (for Caddy/HTTPS)
- Port 8123 (internal, for Home Assistant)

## Usage

```bash
# With custom domain (HTTPS enabled)
sudo ./setup-ha-mcp.sh my-ha-lab.example.com

# Without domain (local access only)
sudo ./setup-ha-mcp.sh ""
```

## Access

After setup:

| Access | URL |
|--------|-----|
| Local | http://localhost:8123 |
| External | https://your-domain.example.com |
| Credentials | dev / dev |

## Logs

```bash
# Home Assistant container logs
docker logs -f $(docker ps --filter "ancestor=ghcr.io/home-assistant/home-assistant" -q)

# Startup script log
tail -f /tmp/hamcp.log
```

## Troubleshooting

### Container not starting
```bash
cat /tmp/hamcp.log
docker ps -a
```

### Caddy certificate issues
```bash
sudo journalctl -u caddy -f
sudo caddy validate --config /etc/caddy/Caddyfile
```

### Restart the environment
```bash
# Stop
docker stop $(docker ps --filter "ancestor=ghcr.io/home-assistant/home-assistant" -q)

# Start
cd ~/ha-mcp && HA_TEST_PORT=8123 ~/.local/bin/uv run hamcp-test-env --no-interactive &
```
