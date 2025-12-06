---
name: Windows
icon: windows
order: 3
---

## Install uv (Package Manager)

uv is a fast Python package manager required to run ha-mcp.

### With WinGet (Recommended)

Open **PowerShell** or **Command Prompt**:

```powershell
winget install astral-sh.uv -e
```

### With PowerShell Script

```powershell
irm https://astral.sh/uv/install.ps1 | iex
```

After installation, **restart your terminal**.

## Verify Installation

```powershell
uvx --version
```

## Quick Test

Try the demo environment:
```powershell
$env:HOMEASSISTANT_URL="https://ha-mcp-demo-server.qc-h.net"
$env:HOMEASSISTANT_TOKEN="demo"
uvx ha-mcp@latest
```

## Troubleshooting

If `uvx` is not found after installation:
1. Close and reopen your terminal
2. Or add uv to your PATH manually
