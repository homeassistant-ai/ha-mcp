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
6. **Crontab Setup** - Auto-starts on reboot + weekly reset (Mondays 3am)
7. **Caddy Reverse Proxy** - HTTPS with automatic Let's Encrypt certificates
8. **Unattended Upgrades** - Auto-updates system packages with auto-reboot at 4am
9. **Container Cleanup** - Removes old HA containers
10. **Start Test Environment** - Launches hamcp-test-env in background

## Weekly Reset (Monday 3am)

The lab environment automatically resets every Monday at 3am:
- Pulls latest ha-mcp changes from git
- Stops and removes Home Assistant containers
- Prunes unused Docker images (prevents disk fill)
- Restarts hamcp-test-env with fresh container

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

## Multi-Server Setup (DNS Round-Robin)

You can run multiple lab servers behind DNS round-robin or a network load balancer for redundancy. Run the setup script on each node with the same domain.

**Prerequisites:**
- Domain must resolve to all server IPs (A/AAAA records for each node)
- Ports 80 and 443 open on all nodes (for Let's Encrypt HTTP-01 challenge and HTTPS)

**How it works:** Caddy uses HTTP-01 challenge for Let's Encrypt certificates. Each node will retry until challenge traffic reaches it.

| Nodes | Success chance per attempt | Notes |
|-------|---------------------------|-------|
| 2 | 50% | Works well |
| 3-4 | 25-33% | Still practical |
| 5+ | <20% | Consider alternatives below |

**For 5+ nodes**, consider:
- **DNS-01 challenge** - Any node can prove ownership via DNS TXT record
- **TLS termination at LB** - Single cert on load balancer, HTTP to backends
- **Shared certificate** - One node obtains cert, distribute to others

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
