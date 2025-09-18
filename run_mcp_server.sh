#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

cd "$SCRIPT_DIR"
#echo "Working directory → $PWD"

#echo "Setting up Home Assistant MCP Server with uv (Linux)..."
export UV_PROJECT_ENVIRONMENT=".venv.linux"

#echo "Installing dependencies with uv project workflow..."
uv sync -q

#echo "Running homeassistant-mcp entry point..."
uv run -q homeassistant-mcp