# ha-mcp Lab Setup

This directory contains scripts for setting up a persistent Home Assistant test environment, useful for demos, development, and integration testing.

## Requirements

- Ubuntu/Debian Linux server (tested on Debian 12)
- Minimum specs: **2 vCPU, 4GB RAM, 20GB disk** (Home Assistant uses ~1.5GB RAM; 6GB swap is created automatically for headroom)
- A non-root user with `sudo` access
- Domain name pointing to the server's IP (for HTTPS; optional for local-only)
- Ports **80** and **443** open inbound (for Caddy + Let's Encrypt)

## Quick Start

```bash
# 1. SSH in as a non-root user (e.g. the default GCP/AWS/Azure user)
# 2. Download and run:
curl -fsSL https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/master/tests/lab-setup/setup-ha-mcp.sh -o setup-ha-mcp.sh
chmod +x setup-ha-mcp.sh
sudo ./setup-ha-mcp.sh your-domain.example.com
```

> **Important:** Use `sudo ./setup-ha-mcp.sh`, not `sudo su -` followed by `./setup-ha-mcp.sh`.
> The script uses `$SUDO_USER` to identify which user to configure. Running as root directly loses that information and the script will exit with an error.
>
> If calling from a **root cron job**, set it explicitly:
> `SUDO_USER=youruser ./setup-ha-mcp.sh your-domain.example.com`

Setup takes about 3–10 minutes on subsequent runs. Allow up to 15 minutes on first install when the ~600 MB HA image must be pulled. Home Assistant starts automatically.

## What It Does

The setup script is **idempotent** (safe to re-run) and performs:

1. **Swap** — Creates 6GB swap for small VMs
2. **Packages** — Installs curl, git, ca-certificates, gnupg
3. **Docker** — Via official get.docker.com script
4. **uv** — Python package manager for running ha-mcp
5. **ha-mcp repo** — Clones to `~/ha-mcp` (or pulls if it already exists)
6. **Systemd service** — Creates `hamcp-demo.service` (starts on boot, exits+restarts if HA container dies) and two timers: `hamcp-demo-reset.timer` (daily at 19:00 UTC: fresh HA restart, 2h before nightly CI check) and `hamcp-demo-update.timer` (daily at 03:00 UTC: git pull, docker image prune, service restart)
7. **Caddy** — Reverse proxy with automatic Let's Encrypt TLS for your domain
8. **Unattended upgrades** — Auto-updates OS packages, reboots at 4am if needed
9. **Container cleanup** — Removes stale HA containers and any leaked processes
10. **Start** — Launches `hamcp-demo` via systemd and waits for Home Assistant to become ready

## Access

After setup:

| | URL |
|---|---|
| Local | http://localhost:8123 |
| External | https://your-domain.example.com |
| Credentials | `dev` / `dev` |

## Managing the Service

```bash
# Status
sudo systemctl status hamcp-demo

# Restart (e.g. after manual code changes)
sudo systemctl restart hamcp-demo

# Live logs
sudo journalctl -u hamcp-demo -f

# Daily update timer (03:00 UTC) — next run and last result
sudo systemctl list-timers hamcp-demo-update.timer
sudo journalctl -u hamcp-demo-update --no-pager -n 20

# Daily reset timer (19:00 UTC) — next run and last result
sudo systemctl list-timers hamcp-demo-reset.timer
sudo journalctl -u hamcp-demo-reset --no-pager -n 10
```

## Logs

```bash
# Service log (startup, HA output, errors)
sudo journalctl -u hamcp-demo -f

# Home Assistant container logs
docker logs -f $(docker ps --filter "ancestor=ghcr.io/home-assistant/home-assistant" -q)
```

## Troubleshooting

### Home Assistant not starting
```bash
sudo journalctl -u hamcp-demo --no-pager -n 50
docker ps -a
```

### Caddy certificate issues
```bash
sudo journalctl -u caddy --no-pager -n 30
# Force renewal by restarting Caddy
sudo systemctl restart caddy
```

### Restart the environment
```bash
sudo systemctl restart hamcp-demo
```

### Full reset (re-clone and restart from scratch)
```bash
sudo systemctl stop hamcp-demo
docker ps -aq --filter "ancestor=ghcr.io/home-assistant/home-assistant" | xargs -r docker rm -f 2>/dev/null || true
docker ps -aq --filter "ancestor=testcontainers/ryuk" | xargs -r docker rm -f 2>/dev/null || true
rm -rf ~/ha-mcp
sudo ./setup-ha-mcp.sh your-domain.example.com
```
