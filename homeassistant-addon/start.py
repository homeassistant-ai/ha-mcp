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


def log_warning(message: str) -> None:
    """Log warning message."""
    _log_with_timestamp("WARNING", message, sys.stderr)


def log_error(message: str) -> None:
    """Log error message."""
    _log_with_timestamp("ERROR", message, sys.stderr)


def widen_fastmcp_log_console(width: int = 200) -> None:
    """Widen FastMCP's rich log consoles so the MCP URL never wraps.

    Rich defaults to an 80-column console when stderr is not a TTY (as in
    the add-on container), which wraps FastMCP's own "Starting MCP server
    ... on http://0.0.0.0:9583/<secret>" line in the middle of the secret
    path. A secret split across lines survives the find/replace users do
    to sanitize logs before sharing them (#1918).
    """
    import logging

    from rich.logging import RichHandler

    widened = 0
    for handler in logging.getLogger("fastmcp").handlers:
        if isinstance(handler, RichHandler):
            handler.console.width = width
            widened += 1
    if not widened:
        log_warning(
            "No rich handlers found on the fastmcp logger — "
            "the MCP URL may wrap across log lines"
        )


def generate_secret_path() -> str:
    """Generate a secure random path with 128-bit entropy.

    Format: /private_<22-char-urlsafe-token>
    Example: /private_zctpwlX7ZkIAr7oqdfLPxw
    """
    return "/private_" + secrets.token_urlsafe(16)


_SECRET_PATH_RE = re.compile(r"^/(?!.*://)\S{7,}$")
_SECRET_PATH_HINT = (
    "Path must start with '/', contain no '://', and be at least 8 characters."
)


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
            log_error(
                f"Custom secret path is invalid ({path!r}), ignoring. {_SECRET_PATH_HINT}"
            )
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
                log_error(
                    f"Stored secret path is invalid ({stored_path!r}), regenerating. {_SECRET_PATH_HINT}"
                )
            else:
                log_error("Stored secret path is empty, regenerating")
        except Exception as e:  # noqa: BLE001
            log_error(f"Failed to read stored secret path: {e}")

    # Generate new secret path
    new_path = generate_secret_path()
    log_info("Generated new secret path with 128-bit entropy")
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(new_path)
        return new_path
    except Exception as e:  # noqa: BLE001
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


# Generated from ha_mcp.config_settings by scripts/generate_app_options.py.
# start.py reads this table instead of importing ha_mcp: importing the package
# builds the server's settings before the options below are exported.
APP_OPTIONS_PATH = Path(__file__).with_name("app_options.json")
BETA_MASTER = "enable_beta_features"


def load_app_options(path: Path = APP_OPTIONS_PATH) -> list[dict[str, Any]]:
    """Return the app option table: key, env var, type, default and limits."""
    options: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    return options


def _option_problem(option: dict[str, Any], raw: Any) -> str | None:
    """Return why ``raw`` is not a valid value for ``option``, or None."""
    kind = option["type"]
    if kind == "bool" and not isinstance(raw, bool):
        return "expected true or false"
    if kind == "int" and (isinstance(raw, bool) or not isinstance(raw, int)):
        return "expected a whole number"
    if kind == "str" and not isinstance(raw, str):
        return "expected text"
    if option["range"] is not None:
        lo, hi = option["range"]
        if not lo <= raw <= hi:
            return f"expected {lo} through {hi}"
    if option["choices"] is not None and raw not in option["choices"]:
        return f"expected one of {', '.join(option['choices'])}"
    return None


def resolve_option(config: dict[str, Any], option: dict[str, Any]) -> Any:
    """Return the option's value from ``config``, or a fallback.

    An absent key gives the default silently. A value of the wrong type or
    outside its limits gives the option's ``invalid`` value, which is the
    default except for a safety control that fails closed, with a warning:
    Supervisor validates the schema, so such a value means a hand-edited
    options.json.
    """
    key = option["key"]
    if key not in config:
        return option["default"]
    raw = config[key]
    problem = _option_problem(option, raw)
    if problem is None:
        return raw
    fallback = option["invalid"]
    log_warning(
        f"app option {key!r} has invalid value {raw!r} ({problem}); using {fallback!r}."
    )
    return fallback


def export_app_options(
    config: dict[str, Any], options: list[dict[str, Any]]
) -> dict[str, Any]:
    """Export each app option to its env var and return the exported values.

    An option every flavor declares is always exported, with its default
    when ``options.json`` lacks it. An option only some flavors declare
    (the dev-only beta keys) is exported only when ``options.json`` has
    it: on the stable flavor an exported value would mark the setting as
    app-managed in the web UI, and Supervisor would reject the eventual
    save of a key the stable schema does not declare.
    """
    exported: dict[str, Any] = {}
    for option in options:
        if not option["always"] and option["key"] not in config:
            continue
        value = resolve_option(config, option)
        os.environ[option["env"]] = (
            str(value).lower() if isinstance(value, bool) else str(value)
        )
        exported[option["key"]] = value
    return exported


def beta_subflags(options: list[dict[str, Any]]) -> list[str]:
    """Return the dev-only beta sub-flag keys: the toggles the beta master gates."""
    return [
        option["key"]
        for option in options
        if not option["always"]
        and option["type"] == "bool"
        and option["key"] != BETA_MASTER
    ]


def resolve_effective_log_level() -> int:
    """Return the root log level from ha-mcp's effective settings.

    Reads the web Settings UI "Log level" advanced setting (persisted
    under ``/data`` and applied by ``get_global_settings()``) so the
    addon log actually honors it — ``main()`` configures logging before
    ha-mcp is imported, so it can only hardcode INFO at that point and
    must re-apply the real level after the import (#1721).

    Call only after ``main()`` has exported ``HOMEASSISTANT_URL`` /
    ``HOMEASSISTANT_TOKEN``: ``get_global_settings()`` caches a
    singleton, so an earlier call would pin placeholder connection
    settings for the process. Any failure falls back to INFO — logging
    config must never block addon startup.
    """
    import logging

    try:
        from ha_mcp.config import get_global_settings

        return getattr(logging, get_global_settings().log_level, logging.INFO)
    except Exception as e:  # noqa: BLE001
        # Loud fallback: without this line, a user who set DEBUG in the
        # web UI can't tell "I'm on INFO" from "my DEBUG request crashed
        # on load" — the same silent-no-op class this fix exists to kill.
        # print-based so it reaches the addon log regardless of logging
        # state.
        log_warning(
            f"Could not resolve effective log level from settings; "
            f"defaulting to INFO (web-UI Log level not applied): {e!r}"
        )
        return logging.INFO


def maybe_auto_enable_beta_master(config: dict[str, Any], subflags: list[str]) -> None:
    """Auto-write ``ENABLE_BETA_FEATURES=true`` when the dev-addon
    options have at least one beta sub-flag key set to True.

    The dev addon's ``config.yaml`` is the only addon schema that
    exposes those keys; the stable addon's ``options.json`` never
    carries any of them, so this check distinguishes dev from stable
    cleanly without needing a separate channel marker.

    With ``ENABLE_BETA_FEATURES=true`` set, the runtime master gate
    in ``config._apply_feature_flag_overrides`` becomes a no-op for
    dev-addon users — Supervisor options remain the authoritative
    source for the 5 sub-flags, exactly as in the legacy code.

    Truthiness check (``config.get(key) is True``) is deliberate.
    HA Supervisor persists every schema-declared option into
    ``/data/options.json`` on first start with its default value, so a
    bare presence check (``key in config``) fired immediately on any
    fresh dev-addon install even when every sub-flag was False —
    locking the master to "on" in the web UI with origin=env, which
    the user could not unset from anywhere.

    REMOVAL CANDIDATE: this helper is only called on the legacy
    fallback path in ``main()`` (``beta_master_in_config`` False
    branch). Once every dev-addon user has saved their addon
    Configuration tab at least once after the master-in-schema
    rollout, ``options.json`` will always carry the
    ``enable_beta_features`` key and this function becomes
    unreachable. Delete after one stable release cycle (track via
    the changelog entry that introduces this helper).
    """
    truthy = [key for key in subflags if config.get(key) is True]
    if truthy:
        os.environ["ENABLE_BETA_FEATURES"] = "true"
        log_info(
            "Legacy-bridge auto-enable: writing ENABLE_BETA_FEATURES=true "
            f"because options.json carries truthy sub-flag(s) {', '.join(truthy)} "
            "but no explicit enable_beta_features key. Save the addon "
            "Configuration tab once to materialise the schema default and "
            "this branch will no-op on subsequent boots."
        )


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


def _warn_gated_off_beta_subflags(
    exported: dict[str, Any], subflags: list[str], defaults: dict[str, Any]
) -> None:
    """Warn when the beta master is OFF but sub-flags are turned on in options.

    Dev-upgrade silent-disable warning: if the master is in options.json and is
    False, but a sub-flag is set to true where it defaults to false, the
    runtime gate will force the sub-flag off. Log loudly so an operator who
    had beta tools on before the master-in-schema rollout, then toggled the
    master off after the update, can see why their tools went away.
    """
    if exported.get(BETA_MASTER) is not False:
        return
    gated_off = [
        key for key in subflags if exported.get(key) is True and not defaults[key]
    ]
    if gated_off:
        log_info(
            "Master beta toggle is OFF but these sub-flags are set "
            f"to true in options.json — they will be force-disabled "
            f"at runtime by the master gate: {', '.join(gated_off)}. "
            "Re-enable the master toggle in the addon Configuration "
            "tab (or the web settings UI) to use them."
        )


def _arm_kill_signal_diagnostics_if_debug(effective_log_level: int) -> None:
    """Install kill-signal diagnostics when DEBUG logging is active."""
    import logging

    if effective_log_level != logging.DEBUG:
        return
    log_info("Debug log level active — arming kill-signal diagnostics")
    # Defers SA_SIGINFO install until uvicorn's capture_signals has
    # run. Otherwise uvicorn's signal.signal() call would overwrite
    # our handler before any signal arrived.
    # Wrapped because diagnostics must never block addon startup.
    try:
        from ha_mcp.utils.kill_signal_diagnostics import (
            schedule_install_after_uvicorn,
        )

        schedule_install_after_uvicorn()
    except Exception as e:  # noqa: BLE001
        log_error(f"kill-signal diagnostics install failed: {e!r}; continuing")


def _run_mcp_server(
    mcp: Any,
    bind_host: str,
    port: int,
    secret_path: str,
    uvicorn_config: dict[str, Any],
) -> int:
    """Run the FastMCP HTTP server, returning the process exit code."""
    try:
        log_info("Starting MCP server...")
        if bind_host != "0.0.0.0":
            log_info(f"Bind host overridden via MCP_HOST: {bind_host}")
        # Do not pass log_level here: fastmcp's temporary_log_level would
        # rebuild its rich log handlers at the default 80-column width,
        # undoing widen_fastmcp_log_console (#1918).
        mcp.run(
            transport="http",
            host=bind_host,
            port=port,
            path=secret_path,
            stateless_http=True,
            uvicorn_config=uvicorn_config,
        )
    except KeyboardInterrupt:
        log_info("Interrupted, exiting")
        return 0
    except BaseException as e:  # noqa: BLE001
        # Top-level crash handler: intentionally catch ANY exit (including
        # SystemExit, translated to its code below) so the add-on supervisor
        # always sees a clean process exit code instead of a traceback.
        import traceback

        log_error(f"MCP server crashed: {e}")
        traceback.print_exc(file=sys.stderr)
        # Log the root cause if this exception was chained
        cause = e.__cause__ or e.__context__
        if cause:
            log_error(f"Caused by: {cause}")
            traceback.print_exception(
                type(cause), cause, cause.__traceback__, file=sys.stderr
            )
        if isinstance(e, SystemExit):
            return int(e.code) if isinstance(e.code, int) else 1
        return 1

    log_info("MCP server stopped")
    return 0


def read_options(config_file: Path) -> dict[str, Any]:
    """Return the app options from ``options.json``, or ``{}`` when it is
    missing or unreadable."""
    config: dict[str, Any] = {}
    if config_file.exists():
        try:
            with open(config_file) as f:
                config = json.load(f)
            if not isinstance(config, dict):
                raise ValueError(f"expected a JSON object, got {type(config).__name__}")
        except Exception as e:  # noqa: BLE001
            config = {}
            log_error(f"Failed to read config: {e}, using defaults")
            # Persistent "you lost your features" line so an operator
            # who scrolled past the cryptic exception trace still sees
            # what got silently reset. /data/options.json corruption
            # would otherwise produce a one-line error followed by a
            # working-but-defaulted addon and no other signal.
            log_error(
                "Addon config defaulted: every option (tool_search, "
                "auto_backup_*, beta sub-flags, etc.) reverts to its "
                "addon-schema default this boot. Inspect /data/options.json "
                "and fix or delete it, then restart the addon."
            )
    return config


def export_options_env(config: dict[str, Any]) -> None:
    """Export the app options and the add-on-only settings to env vars."""
    options = load_app_options()
    exported = export_app_options(config, options)
    log_info(f"Backup hint mode: {exported['backup_hint']}")
    log_info(f"Verify SSL: {exported['verify_ssl']}")
    subflags = beta_subflags(options)
    _warn_gated_off_beta_subflags(
        exported, subflags, {option["key"]: option["default"] for option in options}
    )
    if BETA_MASTER not in config:
        # Legacy safety net: dev-addon installs that pre-date the
        # master-in-schema rollout don't carry the key yet, but their
        # truthy sub-flag presence still implies the user wants beta
        # tools on. Keep the auto-enable as a one-cycle bridge until
        # Supervisor merges the new schema default into options.json.
        maybe_auto_enable_beta_master(config, subflags)
    # Persist saved custom tools across addon restarts. /data is the
    # per-addon writable directory mapped by Supervisor and survives
    # add-on updates (but not uninstall/reinstall — users should copy
    # this file out before reinstalling if they want to migrate).
    # Setting this unconditionally is safe: on the stable add-on the
    # tool isn't registered anyway, so the file is never read or
    # written. This path is hardcoded in add-on mode: it is not an
    # app option, so add-on operators have no surface to
    # change it — and /data is the only location that survives add-on
    # updates anyway.
    os.environ.setdefault("CODE_MODE_SAVED_TOOLS_PATH", "/data/saved_tools.json")


def _log_server_url(secret_path: str) -> None:
    """Log the MCP server URL the user copies into their client."""
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


def main() -> int:
    """Start the Home Assistant MCP Server."""
    log_info("Starting Home Assistant MCP Server...")

    # Read configuration from Supervisor
    config_file = Path("/data/options.json")
    data_dir = Path("/data")
    cleanup_stale_migration_marker(data_dir)
    config = read_options(config_file)
    raw_secret_path = config.get("secret_path", "")
    custom_secret_path = raw_secret_path if isinstance(raw_secret_path, str) else ""

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

    # Set up environment for ha-mcp
    os.environ["HOMEASSISTANT_URL"] = "http://supervisor/core"
    export_options_env(config)

    os.environ["HOMEASSISTANT_TOKEN"] = supervisor_token

    log_info(f"Home Assistant URL: {os.environ['HOMEASSISTANT_URL']}")
    log_info("Authentication configured via Supervisor token")

    # Fixed port (internal container port)
    port = 9583

    _log_server_url(secret_path)

    # Configure logging before server start (v3 removed log_level from run())
    import logging

    logging.basicConfig(level=logging.INFO)

    # Import and register browser landing before server start
    log_info("Importing ha_mcp module...")
    from ha_mcp.__main__ import (
        _get_server,
        _get_timestamped_uvicorn_log_config,
        _log_startup_version,
        mcp,
        register_browser_landing,
    )
    from ha_mcp.log_filters import install_sdk_log_filters
    from ha_mcp.settings_ui import register_settings_routes

    # Importing ha_mcp pulled in fastmcp, which attached its rich log
    # handlers — widen them before any URL-bearing line is logged (#1918).
    # Wrapped because log cosmetics must never block addon startup.
    try:
        widen_fastmcp_log_console()
    except Exception as e:  # noqa: BLE001
        log_warning(f"Could not widen fastmcp log console: {e!r}; continuing")

    # Log the ha-mcp version + a self-update banner when a newer release is
    # available. In the add-on that comes from the Supervisor add-on store, not
    # PyPI (see update_check._resolve_update_info's is_running_in_addon branch).
    # The addon runs its own startup here (it doesn't go through
    # __main__.main_web), so without this the ha-mcp banner never reaches the
    # addon logs — only FastMCP's own banner does (via run_async). Mirrors how
    # FastMCP surfaces its update notice in these same startup logs.
    _log_startup_version()

    # Re-apply the effective log level now that ha_mcp is imported —
    # the basicConfig above could only hardcode INFO. Without this, the
    # web Settings UI "Log level" setting never reaches the addon log
    # (#1721).
    effective_log_level = resolve_effective_log_level()
    logging.getLogger().setLevel(effective_log_level)
    # Proof-of-application canary: emitted through the logging system
    # (not print), so it reaches the addon log ONLY when the DEBUG level
    # actually took effect. Support threads can key on this line to tell
    # "user set DEBUG" apart from "DEBUG is really active".
    logging.getLogger("addon_start").debug(
        "Debug logging active (log_level applied from settings)"
    )

    _arm_kill_signal_diagnostics_if_debug(effective_log_level)

    register_browser_landing(mcp, secret_path)
    # Mount settings UI routes both at root (for HA ingress proxy) and
    # under the secret path (for direct port access). See
    # register_settings_routes docstring for the auth model. Use the
    # server's actual FastMCP instance (not the _DeferredMCP wrapper)
    # so mypy doesn't trip over the duck-typed __getattr__ forwarding.
    server_instance = _get_server()
    register_settings_routes(
        server_instance.mcp, server_instance, secret_path=secret_path
    )
    install_sdk_log_filters()

    # fastmcp's DNS-rebinding guard is defaulted off in ha_mcp's _create_server
    # (reached above via _get_server() / the `mcp` proxy, before the app is
    # built), so the addon -- ingress, direct port, and reverse proxies / tunnels
    # on arbitrary hosts -- is covered. See ha_mcp.transport_security.

    # The addon normally binds to 0.0.0.0 so HA Supervisor ingress can
    # reach it inside the container; MCP_HOST override is provided for
    # parity with the standard CLI entry points (see issue #1434).
    bind_host = os.getenv("MCP_HOST", "0.0.0.0")

    return _run_mcp_server(
        mcp,
        bind_host,
        port,
        secret_path,
        # ws="none": uvicorn's default imports the shared websockets package.
        {"log_config": _get_timestamped_uvicorn_log_config(), "ws": "none"},
    )


if __name__ == "__main__":
    sys.exit(main())
