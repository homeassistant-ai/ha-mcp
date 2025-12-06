---
name: macOS
icon: apple
order: 1
---

## Install uv (Package Manager)

uv is a fast Python package manager required to run ha-mcp.

### With Homebrew (Recommended)

```bash
brew install uv
```

### Without Homebrew

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

After installation, restart your terminal or run:
```bash
source ~/.zshrc
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
