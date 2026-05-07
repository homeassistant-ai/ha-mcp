#!/usr/bin/env python3
"""Home Assistant MCP Server Add-on startup script."""

import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO


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


def persist_addon_options(options: dict[str, Any], supervisor_token: str) -> None:
    """POST the full addon options dict to the Supervisor.

    The endpoint is a full-replace validated against the addon schema, so
    callers must pass the complete options dict (not a partial patch).

    Used after auto-generating the secret path so other addons (the
    webhook proxy) can read it from `GET /addons/{slug}/info → options`
    instead of scraping it from addon logs (#941).

    Raises the underlying `urllib.error.HTTPError` / `URLError` / `OSError`
    on failure — callers decide how loudly to surface the problem.
    """
    payload = json.dumps({"options": options}).encode()
    req = urllib.request.Request(
        "http://supervisor/addons/self/options",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {supervisor_token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def maybe_persist_secret_path(
    config: dict[str, Any], secret_path: str, supervisor_token: str
) -> None:
    """Persist `secret_path` into the addon's stored options when needed.

    Only calls `persist_addon_options` when all of these hold:
    - `config` is non-empty. If `/data/options.json` was missing or failed
      to parse, `config` is `{}` and the addon is running off hardcoded
      defaults. Sending a bare `{"secret_path": ...}` in that state would
      be rejected by Supervisor's schema validation (missing required
      `backup_hint`), producing a second misleading error line on top of
      the "Failed to read config" we already logged.
    - The resolved `secret_path` differs from the stored one. Otherwise
      the write is a pure no-op and we'd just add noise on every restart.

    Errors from the POST are caught and logged with an actionable recovery
    message — the addon keeps running, but the user is told exactly which
    value to paste into the Configuration tab if they hit it.
    """
    if not config:
        return
    if secret_path == config.get("secret_path", ""):
        return
    try:
        persist_addon_options({**config, "secret_path": secret_path}, supervisor_token)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
        detail = (
            f"HTTP {e.code}: {e.reason}"
            if isinstance(e, urllib.error.HTTPError)
            else str(e)
        )
        log_error(
            f"Failed to persist secret_path to addon options ({detail}). "
            f"This addon will still run with secret_path={secret_path!r}, "
            "but other addons (e.g. the webhook proxy) cannot auto-discover "
            "it via Supervisor. Workaround: open this addon's Configuration "
            "tab and paste the secret_path above into the 'Secret path override' "
            "field, then save."
        )


def resolve_bool_option(config: dict[str, Any], key: str, default: bool) -> bool:
    """Read ``key`` from ``config`` as a bool, falling back to ``default``.

    Mirrors the ``raw = config.get(key, default); raw if isinstance(raw, bool) else default``
    pattern used inline in ``main()`` for other options. Extracted so the
    verify_ssl plumbing can be unit-tested without standing up the full
    addon container.
    """
    raw = config.get(key, default)
    return raw if isinstance(raw, bool) else default


_STALE_MIGRATION_MARKER = ".skills_as_tools_default_migration_v1"


def cleanup_stale_migration_marker(data_dir: Path) -> None:
    """Remove the one-time enable_skills_as_tools migration marker.

    The marker was created by the previous version's
    ``migrate_skills_as_tools_default`` (removed in #1133). It is now
    unused on every install; cleaning it up prevents permanent ``/data``
    litter for users who upgraded across the toggle removal. ``unlink``
    is best-effort — a stale dotfile is harmless if removal fails.
    """
    marker = data_dir / _STALE_MIGRATION_MARKER
    try:
        marker.unlink(missing_ok=True)
    except OSError as e:
        log_error(
            f"Failed to remove stale migration marker {marker}: {e}. "
            "Safe to ignore — the file is unused."
        )


def main() -> int:
    """Start the Home Assistant MCP Server."""
    log_info("Starting Home Assistant MCP Server...")

    # Read configuration from Supervisor
    config_file = Path("/data/options.json")
    data_dir = Path("/data")
    cleanup_stale_migration_marker(data_dir)
    config: dict[str, Any] = {}
    backup_hint = "normal"  # default
    custom_secret_path = ""  # default
    enable_tool_search = False  # default
    enable_yaml_config_editing = False  # default
    enable_filesystem_tools = False  # default
    enable_custom_component_integration = False  # default
    tool_search_max_results = 5  # default
    disabled_tools_raw = ""  # default
    pinned_tools_raw = ""  # default
    verify_ssl = True  # default
    advanced_debug_logging = False  # default

    if config_file.exists():
        try:
            with open(config_file) as f:
                config = json.load(f)
            backup_hint = config.get("backup_hint", "normal")
            custom_secret_path = config.get("secret_path", "")
            raw_tool_search = config.get("enable_tool_search", False)
            enable_tool_search = raw_tool_search if isinstance(raw_tool_search, bool) else False
            raw_yaml_config = config.get("enable_yaml_config_editing", False)
            enable_yaml_config_editing = raw_yaml_config if isinstance(raw_yaml_config, bool) else False
            raw_filesystem_tools = config.get("enable_filesystem_tools", False)
            enable_filesystem_tools = raw_filesystem_tools if isinstance(raw_filesystem_tools, bool) else False
            raw_custom_component = config.get("enable_custom_component_integration", False)
            enable_custom_component_integration = raw_custom_component if isinstance(raw_custom_component, bool) else False
            raw_max_results = config.get("tool_search_max_results", 5)
            tool_search_max_results = raw_max_results if isinstance(raw_max_results, int) else 5
            raw_disabled = config.get("disabled_tools", "")
            disabled_tools_raw = raw_disabled if isinstance(raw_disabled, str) else ""
            raw_pinned = config.get("pinned_tools", "")
            pinned_tools_raw = raw_pinned if isinstance(raw_pinned, str) else ""
            verify_ssl = resolve_bool_option(config, "verify_ssl", True)
            advanced_debug_logging = resolve_bool_option(config, "advanced_debug_logging", False)
        except Exception as e:
            log_error(f"Failed to read config: {e}, using defaults")

    # Validate Supervisor token (needed for both ha-mcp auth below and the
    # options-persist call right after secret path resolution)
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    if not supervisor_token:
        log_error("SUPERVISOR_TOKEN not found! Cannot authenticate.")
        return 1

    # Generate or retrieve secret path
    secret_path = get_or_create_secret_path(data_dir, custom_secret_path)

    # Persist secret path back to addon options so other addons (e.g. the
    # webhook proxy) can read it via `GET /addons/{slug}/info → options`
    # instead of scraping it from this addon's logs (#941). Details and
    # the skip/retry rules live in maybe_persist_secret_path().
    maybe_persist_secret_path(config, secret_path, supervisor_token)

    log_info(f"Backup hint mode: {backup_hint}")
    log_info(f"Verify SSL: {verify_ssl}")
    log_info(f"Advanced debug logging: {advanced_debug_logging}")

    # Set up environment for ha-mcp
    os.environ["HOMEASSISTANT_URL"] = "http://supervisor/core"
    os.environ["BACKUP_HINT"] = backup_hint
    os.environ["ENABLE_TOOL_SEARCH"] = str(enable_tool_search).lower()
    os.environ["ENABLE_YAML_CONFIG_EDITING"] = str(enable_yaml_config_editing).lower()
    os.environ["HAMCP_ENABLE_FILESYSTEM_TOOLS"] = str(enable_filesystem_tools).lower()
    os.environ["HAMCP_ENABLE_CUSTOM_COMPONENT_INTEGRATION"] = str(enable_custom_component_integration).lower()
    os.environ["TOOL_SEARCH_MAX_RESULTS"] = str(tool_search_max_results)
    os.environ["DISABLED_TOOLS"] = disabled_tools_raw
    os.environ["PINNED_TOOLS"] = pinned_tools_raw
    os.environ["HA_VERIFY_SSL"] = str(verify_ssl).lower()

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
        StatelessSessionLogFilter,
        _get_server,
        _get_timestamped_uvicorn_log_config,
        mcp,
        register_browser_landing,
    )
    from ha_mcp.settings_ui import register_settings_routes

    if advanced_debug_logging:
        # Defers SA_SIGINFO install until uvicorn's capture_signals has
        # run. Otherwise uvicorn's signal.signal() call would overwrite
        # our handler before any signal arrived.
        # Wrapped because diagnostics must never block addon startup.
        try:
            from ha_mcp.utils.kill_signal_diagnostics import (
                schedule_install_after_uvicorn,
            )
            schedule_install_after_uvicorn()
        except Exception as e:
            log_error(f"advanced_debug_logging install failed: {e!r}; continuing")

    register_browser_landing(mcp, secret_path)
    # Mount settings UI routes both at root (for HA ingress proxy) and
    # under the secret path (for direct port access). See
    # register_settings_routes docstring for the auth model. Use the
    # server's actual FastMCP instance (not the _DeferredMCP wrapper)
    # so mypy doesn't trip over the duck-typed __getattr__ forwarding.
    server_instance = _get_server()
    register_settings_routes(server_instance.mcp, server_instance, secret_path=secret_path)
    logging.getLogger("mcp.server.streamable_http").addFilter(
        StatelessSessionLogFilter()
    )

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
