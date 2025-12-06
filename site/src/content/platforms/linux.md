---
name: Linux
icon: linux
order: 2
---

## Install uv (Package Manager)

uv is a fast Python package manager required to run ha-mcp.

### Install Script

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

After installation, restart your terminal or run:
```bash
source ~/.bashrc
# or for zsh:
source ~/.zshrc
```

### Package Managers

**Arch Linux:**
```bash
pacman -S uv
```

**Alpine:**
```bash
apk add uv
```

## Verify Installation

```bash
uvx --version
```

## Quick Test

Try the demo environment:
```bash
HOMEASSISTANT_URL=https://ha-mcp-demo-server.qc-h.net HOMEASSISTANT_TOKEN=demo uvx ha-mcp@latest
```
