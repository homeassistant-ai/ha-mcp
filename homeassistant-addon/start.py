#!/usr/bin/env python3
"""Home Assistant MCP Server Add-on startup script."""

import json
import os
import re
import secrets
import sys
from datetime import datetime
from pathlib import Path
from typing import TextIO


def _log_with_timestamp(level: str, message: str, stream: TextIO | None = None) -> None:
    """Log a message with a timestamp."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{now} [{level}] {message}", file=stream, flush=True)


def log_info(message: str) -> None:
    """Log info message."""
    _log_with_timestamp("INFO", message)


def log_error(message: str) -> None:
    """Log error message."""
    _log_with_timestamp("ERROR", message, sys.stderr)


def generate_secret_path() -> str:
    """Generate a secure random path with 128-bit entropy.

    Format: /private_<22-char-urlsafe-token>
    Example: /private_zctpwlX7ZkIAr7oqdfLPxw
    """
    return "/private_" + secrets.token_urlsafe(16)


_SECRET_PATH_RE = re.compile(r"^/(?!.*://)\S{7,}$")
_SECRET_PATH_HINT = "Path must start with '/', contain no '://', and be at least 8 characters."


def _is_valid_secret_path(path: str) -> bool:
    """Return True if path starts with '/', contains no '://', and is at least 8 characters."""
    return bool(_SECRET_PATH_RE.match(path))


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
        if not _is_valid_secret_path(path):
            log_error(f"Custom secret path is invalid ({path!r}), ignoring. {_SECRET_PATH_HINT}")
        else:
            log_info("Using custom secret path from configuration")
            # Update stored path for consistency
            secret_file.write_text(path)
            return path

    # Check if we have a stored secret path
    if secret_file.exists():
        try:
            stored_path = secret_file.read_text().strip()
            if _is_valid_secret_path(stored_path):
                log_info("Using existing auto-generated secret path")
                return stored_path
            elif stored_path:
                log_error(f"Stored secret path is invalid ({stored_path!r}), regenerating. {_SECRET_PATH_HINT}")
            else:
                log_error("Stored secret path is empty, regenerating")
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
    enable_skills = True  # default
    enable_skills_as_tools = False  # default

    if config_file.exists():
        try:
            with open(config_file) as f:
                config = json.load(f)
            backup_hint = config.get("backup_hint", "normal")
            custom_secret_path = config.get("secret_path", "")
            raw_skills = config.get("enable_skills", True)
            enable_skills = raw_skills if isinstance(raw_skills, bool) else True
            raw_skills_as_tools = config.get("enable_skills_as_tools", False)
            enable_skills_as_tools = raw_skills_as_tools if isinstance(raw_skills_as_tools, bool) else False
        except Exception as e:
            log_error(f"Failed to read config: {e}, using defaults")

    # Generate or retrieve secret path
    secret_path = get_or_create_secret_path(data_dir, custom_secret_path)

    log_info(f"Backup hint mode: {backup_hint}")

    # Set up environment for ha-mcp
    os.environ["HOMEASSISTANT_URL"] = "http://supervisor/core"
    os.environ["BACKUP_HINT"] = backup_hint
    os.environ["ENABLE_SKILLS"] = str(enable_skills).lower()
    os.environ["ENABLE_SKILLS_AS_TOOLS"] = str(enable_skills_as_tools).lower()

    # Validate Supervisor token
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    if not supervisor_token:
        log_error("SUPERVISOR_TOKEN not found! Cannot authenticate.")
        return 1

    os.environ["HOMEASSISTANT_TOKEN"] = supervisor_token

    log_info(f"Home Assistant URL: {os.environ['HOMEASSISTANT_URL']}")
    log_info("Authentication configured via Supervisor token")

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

    # Configure logging before server start (v3 removed log_level from run())
    import logging
    logging.basicConfig(level=logging.INFO)

    # Import and register browser landing before server start
    log_info("Importing ha_mcp module...")
    from ha_mcp.__main__ import (
        _get_timestamped_uvicorn_log_config,
        mcp,
        register_browser_landing,
    )

    register_browser_landing(mcp, secret_path)

    try:
        log_info("Starting MCP server...")
        mcp.run(
            transport="http",
            host="0.0.0.0",
            port=port,
            path=secret_path,
            stateless_http=True,
            uvicorn_config={"log_config": _get_timestamped_uvicorn_log_config()},
        )
    except KeyboardInterrupt:
        log_info("Interrupted, exiting")
        return 0
    except BaseException as e:
        import traceback

        log_error(f"MCP server crashed: {e}")
        traceback.print_exc(file=sys.stderr)
        # Log the root cause if this exception was chained
        cause = e.__cause__ or e.__context__
        if cause:
            log_error(f"Caused by: {cause}")
            traceback.print_exception(type(cause), cause, cause.__traceback__, file=sys.stderr)
        if isinstance(e, SystemExit):
            return int(e.code) if isinstance(e.code, int) else 1
        return 1

    log_info("MCP server stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
