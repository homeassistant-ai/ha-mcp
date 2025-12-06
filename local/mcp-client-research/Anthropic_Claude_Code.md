# Claude Code (Anthropic)

## Overview
Claude Code is Anthropic's CLI tool for AI-assisted coding. It supports MCP servers via command-line configuration.

---

## Source: awesome-remote-mcp-servers

### HTTP/Remote Server
```bash
claude mcp add --transport http "server-name" https://your-server.com/sse
```

---

## Source: MicrosoftDocs/mcp

### HTTP Transport
```bash
claude mcp add --transport http server-name https://your-server.com/api/mcp
```

### Scope Options
- `--scope user` - Enable across all projects (global)
- `--scope local` - Enable for current project only (default)

---

## Source: playwright-mcp

### STDIO Transport (Local Package)
```bash
claude mcp add playwright npx @playwright/mcp@latest
```

---

## Source: github-mcp-server

### With Environment Variables
```bash
claude mcp add github --command docker --args "run -i --rm -e GITHUB_PERSONAL_ACCESS_TOKEN ghcr.io/github/github-mcp-server" -e GITHUB_PERSONAL_ACCESS_TOKEN=your-token
```

---

## Source: Multiple repos (general pattern)

### Basic Commands
```bash
# Add a server
claude mcp add <name> <command> [args...]

# Add with transport type
claude mcp add --transport stdio <name> <command> [args...]
claude mcp add --transport http <name> <url>

# Add with environment variables
claude mcp add <name> <command> -e VAR1=value1 -e VAR2=value2

# Add with scope
claude mcp add <name> <command> --scope user    # Global
claude mcp add <name> <command> --scope local   # Project only

# List configured servers
claude mcp list

# Remove a server
claude mcp remove <name>
```

### Examples
```bash
# NPX package
claude mcp add my-server npx @package/mcp-server

# Python with uvx
claude mcp add my-server uvx package-name

# With API key
claude mcp add my-server npx @package/server -e API_KEY=xxx

# HTTP remote server
claude mcp add --transport http remote-server https://api.example.com/mcp
```

---

## Notes
- Claude Code can install servers from the MCP marketplace
- Use `claude mcp list` to see configured servers
- Supports both stdio and HTTP transports
- Environment variables are stored securely
