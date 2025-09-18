@echo off

REM Changing directory to the script...
cd /D "%~dp0"

REM Setting up Home Assistant MCP Server for Windows with uv...
REM Installing dependencies with uv project workflow...
set UV_PROJECT_ENVIRONMENT=.venv.win
uv sync -q
uv run -q homeassistant-mcp
