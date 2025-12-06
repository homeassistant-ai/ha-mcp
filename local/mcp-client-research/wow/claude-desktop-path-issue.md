# Interesting: Claude Desktop PATH Issue

## Discovery
Claude Desktop does NOT inherit the user's shell PATH environment.

## Problem
If a user installs `uvx` to `~/.local/bin/`, Claude Desktop can't find it because desktop apps don't inherit shell PATH.

## Solution
Use full paths in config:
```json
{
  "mcpServers": {
    "ha": {
      "command": "/Users/username/.local/bin/uvx",
      "args": ["ha-mcp@latest"]
    }
  }
}
```

Or detect path in installer:
```bash
UVX_PATH=$(command -v uvx)
# Then write full path to config
```

## Relevance for ha-mcp
We already fixed this in PR #284 for the macOS installer. Windows may have similar issues.
