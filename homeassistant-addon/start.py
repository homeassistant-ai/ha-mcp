#!/usr/bin/env python3
"""Home Assistant MCP Server Add-on startup script."""

import json
import os
import secrets
import sys
from pathlib import Path


def log_info(message: str) -> None:
    """Log info message."""
    print(f"[INFO] {message}", flush=True)


def log_error(message: str) -> None:
    """Log error message."""
    print(f"[ERROR] {message}", file=sys.stderr, flush=True)


def generate_secret_path() -> str:
    """Generate a secure random path with 128-bit entropy.

    Format: /private_<22-char-urlsafe-token>
    Example: /private_zctpwlX7ZkIAr7oqdfLPxw
    """
    return "/private_" + secrets.token_urlsafe(16)


def get_or_create_secret_path(data_dir: Path, custom_path: str = "") -> str:
    """Get existing secret path or create a new one.

    Args:
        data_dir: Path to the /data directory
        custom_path: Optional custom path from config (overrides auto-generated)

    Returns:
        The secret path to use
    """
    secret_file = data_dir / "secret_path.txt"

    # If custom path is provided, use it and update the stored path
    if custom_path and custom_path.strip():
        path = custom_path.strip()
        if not path.startswith("/"):
            path = "/" + path
        log_info(f"Using custom secret path from configuration")
        # Update stored path for consistency
        secret_file.write_text(path)
        return path

    # Check if we have a stored secret path
    if secret_file.exists():
        try:
            stored_path = secret_file.read_text().strip()
            if stored_path:
                log_info(f"Using existing auto-generated secret path")
                return stored_path
        except Exception as e:
            log_error(f"Failed to read stored secret path: {e}")

    # Generate new secret path
    new_path = generate_secret_path()
    log_info("Generated new secret path with 128-bit entropy")
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(new_path)
        return new_path
    except Exception as e:
        log_error(f"Failed to save secret path: {e}")
        # Return the path anyway - it will work for this session
        return new_path


def main() -> int:
    """Start the Home Assistant MCP Server."""
    log_info("Starting Home Assistant MCP Server...")

    # Read configuration from Supervisor
    config_file = Path("/data/options.json")
    data_dir = Path("/data")
    backup_hint = "normal"  # default
    custom_secret_path = ""  # default

    if config_file.exists():
        try:
            with open(config_file) as f:
                config = json.load(f)
            backup_hint = config.get("backup_hint", "normal")
            custom_secret_path = config.get("secret_path", "")
        except Exception as e:
            log_error(f"Failed to read config: {e}, using defaults")

    # Generate or retrieve secret path
    secret_path = get_or_create_secret_path(data_dir, custom_secret_path)

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

    # Debug: Test Supervisor API connectivity
    try:
        import asyncio

        import httpx

        async def debug_supervisor() -> None:
            url = os.environ["HOMEASSISTANT_URL"]
            token = os.environ["HOMEASSISTANT_TOKEN"]
            log_info(f"DEBUG: Testing Supervisor API at {url}")

            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {token}"}
            ) as client:
                # Test 1: GET / (Core root via proxy)
                # Note: http://supervisor/core might redirect or return 404/401 depending on path
                try:
                    target = f"{url}/api/"
                    resp = await client.get(target)
                    log_info(f"DEBUG: GET {target} -> {resp.status_code}")
                except Exception as e:
                    log_error(f"DEBUG: GET {target} failed: {e}")

                # Test 2: DELETE /api/config/automation/config/test_delete_debug
                # This mimics the failing call
                target = f"{url}/api/config/automation/config/test_delete_debug"
                log_info(f"DEBUG: Attempting DELETE {target}")
                try:
                    resp = await client.delete(target)
                    log_info(
                        f"DEBUG: DELETE result -> {resp.status_code} {resp.text[:200]}"
                    )
                except Exception as e:
                    log_error(f"DEBUG: DELETE failed: {e}")

        asyncio.run(debug_supervisor())
    except Exception as e:
        log_error(f"DEBUG setup failed: {e}")

    # Fixed port (internal container port)
    port = 9583

    log_info("")
    log_info("=" * 80)
    log_info(f"🔐 MCP Server URL: http://<home-assistant-ip>:9583{secret_path}")
    log_info("")
    log_info(f"   Secret Path: {secret_path}")
    log_info("")
    log_info("   ⚠️  IMPORTANT: Copy this exact URL - the secret path is required!")
    log_info("   💡 This path is auto-generated and persisted to /data/secret_path.txt")
    log_info("=" * 80)
    log_info("")

    # Import and run MCP server directly
    try:
        try:
            from importlib.metadata import version

            pkg_version = version("ha-mcp")
            log_info(f"📦 ha-mcp package version: {pkg_version}")
        except Exception:
            log_info("📦 ha-mcp package version: unknown")

        log_info("Importing ha_mcp module...")
        from ha_mcp.__main__ import mcp

        log_info("Starting MCP server...")
        mcp.run(
            transport="streamable-http",
            host="0.0.0.0",
            port=port,
            path=secret_path,
            log_level="info",
        )
    except Exception as e:
        log_error(f"Failed to start MCP server: {e}")
        import traceback

        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
