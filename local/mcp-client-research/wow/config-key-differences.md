# Interesting: Configuration Key Differences

## Discovery
Different clients use different JSON keys for the same concepts.

## URL Keys for HTTP Servers

| Client | Key Used |
|--------|----------|
| Cursor | `url` |
| VS Code | `url` |
| Windsurf | `serverUrl` |
| Gemini CLI | `httpUrl` |
| Qwen Code | `httpUrl` |
| Cline | `url` + `type` |

## Type Fields

| Client | Type Field |
|--------|------------|
| VS Code | `"type": "stdio"` or `"type": "http"` |
| GitHub Copilot | `"type": "local"` or `"type": "http"` |
| Cline | `"type": "streamableHttp"` for remote |

## Config Formats

| Client | Format |
|--------|--------|
| Most clients | JSON |
| Codex | TOML |

## Relevance for ha-mcp
When documenting setup instructions, we need client-specific examples showing the correct keys for each client.
