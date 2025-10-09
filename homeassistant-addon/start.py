#!/usr/bin/env python3
"""Home Assistant MCP Server Add-on startup script."""

import json
import os
import sys
from pathlib import Path


def log_info(message: str) -> None:
    """Log info message."""
    print(f"[INFO] {message}", flush=True)


def log_error(message: str) -> None:
    """Log error message."""
    print(f"[ERROR] {message}", file=sys.stderr, flush=True)


def main() -> int:
    """Start the Home Assistant MCP Server."""
    log_info("Starting Home Assistant MCP Server...")

    # Read configuration from Supervisor
    config_file = Path("/data/options.json")
    backup_hint = "normal"  # default

    if config_file.exists():
        try:
            with open(config_file) as f:
                config = json.load(f)
            backup_hint = config.get("backup_hint", "normal")
        except Exception as e:
            log_error(f"Failed to read config: {e}, using defaults")

    log_info(f"Backup hint mode: {backup_hint}")

    # Set up environment for ha-mcp
    os.environ["HOMEASSISTANT_URL"] = "http://supervisor/core"
    os.environ["BACKUP_HINT"] = backup_hint

    # Validate Supervisor token
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    if not supervisor_token:
        log_error("SUPERVISOR_TOKEN not found! Cannot authenticate.")
        return 1

    os.environ["HOMEASSISTANT_TOKEN"] = supervisor_token

    log_info(f"Home Assistant URL: {os.environ['HOMEASSISTANT_URL']}")
    log_info("Authentication configured via Supervisor token")
    log_info("Launching ha-mcp...")

    # Replace current process with ha-mcp
    os.execvp("ha-mcp", ["ha-mcp"])


if __name__ == "__main__":
    sys.exit(main())
