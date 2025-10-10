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
    port = 9583  # default
    path = "/mcp"  # default
    require_auth = False  # default

    if config_file.exists():
        try:
            with open(config_file) as f:
                config = json.load(f)
            backup_hint = config.get("backup_hint", "normal")
            port = config.get("port", 9583)
            path = config.get("path", "/mcp")
            require_auth = config.get("require_auth", False)
        except Exception as e:
            log_error(f"Failed to read config: {e}, using defaults")

    log_info(f"Backup hint mode: {backup_hint}")
    log_info(f"HTTP port: {port}")
    log_info(f"MCP path: {path}")
    log_info(f"Require authentication: {require_auth}")

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

    # Configure MCP authentication if required
    if require_auth:
        log_info("MCP Authentication: ENABLED (HA token validation)")
        os.environ["FASTMCP_SERVER_AUTH"] = "ha_mcp.auth.ha_token_verifier.HATokenVerifier"
    else:
        log_info("MCP Authentication: DISABLED (use secret path for security)")
    log_info(f"Launching ha-mcp in HTTP mode on 0.0.0.0:{port}{path}")
    log_info("")
    log_info("=" * 70)
    log_info(f"MCP Server URL: http://<home-assistant-ip>:{port}{path}")
    log_info("=" * 70)
    log_info("")

    # Replace current process with fastmcp in streamable-http mode
    os.execvp(
        "fastmcp",
        [
            "fastmcp",
            "run",
            "--transport",
            "streamable-http",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--path",
            path,
        ],
    )


if __name__ == "__main__":
    sys.exit(main())
