# ha-mcp installer for Windows
# Usage: irm https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/master/scripts/install-windows.ps1 | iex
# Or: Invoke-WebRequest -Uri "..." -OutFile install.ps1; .\install.ps1

$ErrorActionPreference = "Stop"

# Configuration
$ConfigDir = "$env:APPDATA\Claude"
$ConfigFile = "$ConfigDir\claude_desktop_config.json"
$DemoUrl = "https://ha-mcp-demo-server.qc-h.net"
$DemoToken = "demo"

Write-Host ""
Write-Host "============================================" -ForegroundColor Blue
Write-Host "   ha-mcp Installer for Windows" -ForegroundColor Blue
Write-Host "============================================" -ForegroundColor Blue
Write-Host ""

# Step 1: Check/install uv
Write-Host "Step 1: Checking for uv..." -ForegroundColor Yellow
$uvInstalled = $null
try {
    $uvInstalled = Get-Command uvx -ErrorAction SilentlyContinue
} catch {}

if ($uvInstalled) {
    Write-Host "  uv is already installed" -ForegroundColor Green
} else {
    Write-Host "  Installing uv..."
    try {
        winget install astral-sh.uv -e --accept-source-agreements --accept-package-agreements
        # Refresh PATH
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
        Write-Host "  uv installed successfully" -ForegroundColor Green
    } catch {
        Write-Host "  Failed to install uv via winget." -ForegroundColor Red
        Write-Host "  Please install manually:" -ForegroundColor Red
        Write-Host "  winget install astral-sh.uv" -ForegroundColor Cyan
        Write-Host "  OR download from: https://docs.astral.sh/uv/" -ForegroundColor Cyan
        exit 1
    }
}
Write-Host ""

# Step 2: Configure Claude Desktop
Write-Host "Step 2: Configuring Claude Desktop..." -ForegroundColor Yellow
$ClaudeNotInstalled = $false
if (-not (Test-Path $ConfigDir)) {
    $ClaudeNotInstalled = $true
    Write-Host "  Claude Desktop not yet installed - creating config for later" -ForegroundColor White
}

# Create config directory if needed
if (-not (Test-Path $ConfigDir)) {
    New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
}

# The MCP server config
$HaMcpConfig = @{
    command = "uvx"
    args = @("ha-mcp@latest")
    env = @{
        HOMEASSISTANT_URL = $DemoUrl
        HOMEASSISTANT_TOKEN = $DemoToken
    }
}

# The properly formatted JSON config
$JsonConfig = @"
{
  "mcpServers": {
    "Home Assistant": {
      "command": "uvx",
      "args": ["ha-mcp@latest"],
      "env": {
        "HOMEASSISTANT_URL": "$DemoUrl",
        "HOMEASSISTANT_TOKEN": "$DemoToken"
      }
    }
  }
}
"@

# Check if config file exists
if (Test-Path $ConfigFile) {
    # Backup existing config
    $BackupFile = "$ConfigFile.backup.$(Get-Date -Format 'yyyyMMdd_HHmmss')"
    Copy-Item $ConfigFile $BackupFile
    Write-Host "  Backed up existing config to:" -ForegroundColor White
    Write-Host "  $BackupFile" -ForegroundColor Cyan

    # Check if already configured
    $content = Get-Content $ConfigFile -Raw -ErrorAction SilentlyContinue
    if ($content -match '"Home Assistant"') {
        Write-Host "  Home Assistant MCP already configured." -ForegroundColor Yellow
        Write-Host "  Updating configuration..." -ForegroundColor White
    }

    # For simplicity, we write the clean config (merging would require complex JSON handling)
    # The backup preserves any other MCP servers the user had
    $JsonConfig | Set-Content $ConfigFile -Encoding UTF8
    Write-Host "  Configuration updated successfully" -ForegroundColor White
} else {
    # Create new config file
    $JsonConfig | Set-Content $ConfigFile -Encoding UTF8
    Write-Host "  Created new configuration file" -ForegroundColor White
}
Write-Host "  Claude Desktop configured" -ForegroundColor Green
Write-Host ""

# Step 3: Pre-download dependencies
Write-Host "Step 3: Pre-downloading ha-mcp..." -ForegroundColor Yellow
Write-Host "  This speeds up Claude Desktop startup..."
try {
    & uvx ha-mcp@latest --help 2>&1 | Out-Null
    Write-Host "  Dependencies cached" -ForegroundColor Green
} catch {
    Write-Host "  Pre-download skipped (will download on first use)" -ForegroundColor Yellow
}
Write-Host ""

# Success message
Write-Host "============================================" -ForegroundColor Green
Write-Host "   Installation Complete!" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host ""
if ($ClaudeNotInstalled) {
    Write-Host "  1. Download Claude Desktop: " -NoNewline
    Write-Host "https://claude.ai/download" -ForegroundColor Cyan
    Write-Host "  2. Create a free account at claude.ai (if you haven't)"
    Write-Host '  3. Open Claude Desktop and ask: "Can you see my Home Assistant?"'
} else {
    Write-Host "  1. Exit Claude Desktop: File > Exit (or right-click system tray > Exit)"
    Write-Host '  2. Reopen and ask: "Can you see my Home Assistant?"'
}
Write-Host ""
Write-Host "Note: " -ForegroundColor Yellow -NoNewline
Write-Host "If Claude Desktop was already running, you must restart it"
Write-Host "      to load the new configuration."
Write-Host ""
Write-Host "Demo environment:" -ForegroundColor Cyan
Write-Host "  Web UI: $DemoUrl"
Write-Host "  Login:  mcp / mcp"
Write-Host "  (Resets weekly - changes won't persist)"
Write-Host ""
Write-Host "To use YOUR Home Assistant:" -ForegroundColor Yellow
Write-Host "  Edit: $ConfigFile"
Write-Host "  Replace HOMEASSISTANT_URL with your HA URL"
Write-Host "  Replace HOMEASSISTANT_TOKEN with your token"
Write-Host "  (Generate token in HA: Profile > Security > Long-lived tokens)"
Write-Host ""
