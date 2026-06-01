#!/bin/sh
# ha-mcp installer for Linux — kept for discoverability.
# install.sh handles both macOS and Linux; use it directly:
# curl -LsSf https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/master/scripts/install.sh | sh
set -e
TMPFILE=$(mktemp)
trap 'rm -f "$TMPFILE"' EXIT
curl -LsSf https://raw.githubusercontent.com/homeassistant-ai/ha-mcp/master/scripts/install.sh -o "$TMPFILE"
sh "$TMPFILE" "$@"
