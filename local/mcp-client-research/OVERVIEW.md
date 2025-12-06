# MCP Client Research Overview

## Research Summary
Analyzed 100 top MCP repos from GitHub to identify MCP client configuration patterns.

## Total Clients Documented: 22

### Tier 1 - Highest Accuracy (5/5)
Consistent across 30+ repos, highly reliable instructions.

| Client | Company | Config Format | Transport |
|--------|---------|---------------|-----------|
| Claude Desktop | Anthropic | JSON | stdio, HTTP (via proxy) |
| Cursor | Anysphere | JSON | stdio, HTTP |
| VS Code | Microsoft | JSON | stdio, HTTP |

### Tier 2 - High Accuracy (4/5)
Consistent across 10-20 repos.

| Client | Company | Config Format | Transport |
|--------|---------|---------------|-----------|
| Claude Code | Anthropic | CLI | stdio, HTTP |
| Windsurf | Codeium | JSON | stdio, HTTP |
| Cline | Extension | JSON | stdio, streamableHttp |

### Tier 3 - Medium Accuracy (3/5)
Consistent across 5-10 repos.

| Client | Company | Config Format | Transport |
|--------|---------|---------------|-----------|
| Codex | OpenAI | TOML | stdio, HTTP |
| Gemini CLI | Google | JSON | HTTP |
| GitHub Copilot | GitHub/Microsoft | JSON | stdio, HTTP |

### Tier 4 - Lower Accuracy (2/5)
Limited documentation, 1-4 repos.

| Client | Company | Notes |
|--------|---------|-------|
| ChatGPT | OpenAI | Web UI only |
| Visual Studio | Microsoft | Built-in |
| JetBrains IDEs | JetBrains | Via AI Assistant |
| Zed | Zed Industries | Standard format |
| Amp | - | Standard format |
| LM Studio | - | Standard format |
| Goose | - | Extensions UI |
| Roo Code | - | VS Code ext |
| Qwen Code | Alibaba | httpUrl key |
| Kiro | - | Via mcp-remote |
| Amazon Q | AWS | Standard format |
| Continue.dev | - | Standard format |
| Warp | - | Standard format |

## Key Findings

### Universal Pattern
Most clients use this JSON structure:
```json
{
  "mcpServers": {
    "server-name": {
      "command": "npx|uvx|python",
      "args": ["package-name"],
      "env": {
        "API_KEY": "value"
      }
    }
  }
}
```

### Key Differences
1. **URL keys vary**: `url`, `serverUrl`, `httpUrl`
2. **Type fields vary**: `stdio`, `http`, `local`, `streamableHttp`
3. **Codex uses TOML** instead of JSON
4. **VS Code supports input prompts** for secure credentials

### Transport Support
- **STDIO**: Most widely supported
- **HTTP/SSE**: Growing support
- **mcp-remote**: Bridge for stdio-only clients

## Files Structure
```
local/mcp-client-research/
├── OVERVIEW.md                    # This file
├── repos.json                     # 100 analyzed repos
├── {Company}_{Product}.md         # 22 individual client files
├── summary/
│   ├── 5.*.md                     # Accuracy 5/5 clients
│   ├── 4.*.md                     # Accuracy 4/5 clients
│   ├── 3.*.md                     # Accuracy 3/5 clients
│   └── 2.*.md                     # Accuracy 2/5 clients
└── wow/
    ├── mcp-remote-proxy.md        # HTTP via proxy pattern
    ├── smithery-one-click.md      # One-click installation
    ├── transport-types.md         # Transport comparison
    ├── config-key-differences.md  # Key naming differences
    ├── input-prompts-pattern.md   # Secure credential pattern
    └── claude-desktop-path-issue.md # PATH issue we fixed
```

## Recommendations for ha-mcp

1. **Prioritize documentation for Tier 1 clients** (Claude Desktop, Cursor, VS Code)
2. **Support both stdio and HTTP** for maximum compatibility
3. **Consider Smithery registration** for one-click install
4. **Document client-specific keys** (url vs serverUrl vs httpUrl)
5. **Include input prompt examples** for VS Code
