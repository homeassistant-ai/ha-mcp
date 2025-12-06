# Codex (OpenAI)

## Overview
OpenAI Codex CLI supports MCP servers via TOML configuration file.

## Configuration File Location
- `~/.codex/config.toml`

---

## Source: MicrosoftDocs/mcp

### HTTP Remote Configuration
Edit `~/.codex/config.toml`:
```toml
[mcp_servers.server-name]
url = "https://your-server.com/api/mcp"
```

---

## Source: playwright-mcp

### CLI Installation
```bash
codex mcp add playwright npx "@playwright/mcp@latest"
```

### TOML Configuration
Edit `~/.codex/config.toml`:
```toml
[mcp_servers.playwright]
command = "npx"
args = ["@playwright/mcp@latest"]
```

---

## Source: Multiple repos (general pattern)

### STDIO Configuration (TOML)
```toml
[mcp_servers.server-name]
command = "npx"
args = ["@package/mcp-server", "--option"]

[mcp_servers.server-name.env]
API_KEY = "your-key"
OTHER_VAR = "value"
```

### HTTP Configuration (TOML)
```toml
[mcp_servers.remote-server]
url = "https://your-server.com/api/mcp"
```

### CLI Commands
```bash
# Add a server
codex mcp add <name> <command> [args...]

# List servers
codex mcp list

# Remove a server
codex mcp remove <name>
```

---

## Notes
- Uses TOML format (different from JSON used by most other clients)
- Configuration file at `~/.codex/config.toml`
- Supports both CLI and file-based configuration
- Supports both stdio and HTTP transports
