#!/bin/sh
# ha-mcp installer for macOS — kept for backward compatibility.
# install.sh now handles both macOS and Linux; use it directly:
# curl -LsSf https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/master/scripts/install.sh | sh
set -e
TMPFILE=$(mktemp)
trap 'rm -f "$TMPFILE"' EXIT
curl -LsSf https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/master/scripts/install.sh -o "$TMPFILE"
sh "$TMPFILE" "$@"
