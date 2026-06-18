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
    if isinstance(raw, bool):
        return raw
    if key in config:
        # Present-but-wrong-type is the diagnostic case: HA Supervisor
        # coerces YAML scalars to the schema type, so a non-bool here
        # usually means a hand-edited options.json with a bad value. Warn
        # so the operator sees why the secure default won (an absent key
        # is the normal path and stays silent).
        log_warning(
            f"addon option {key!r} has type {type(raw).__name__} "
            f"(expected bool); applying default {default!r}."
        )
    return default


_DEV_ADDON_BETA_KEYS = (
    "enable_yaml_config_editing",
    # Per-key sub-gates of enable_yaml_config_editing. Kept in lockstep
    # with config.BETA_FEATURE_FIELDS (enforced by
    # test_auto_enable_keys_match_BETA_FEATURE_FIELDS_registry) so the
    # auto-enable bridge covers exactly the runtime beta set.
    "enable_yaml_packages_automation",
    "enable_yaml_packages_script",
    "enable_yaml_packages_scene",
    "enable_filesystem_tools",
    "enable_custom_component_integration",
    "enable_code_mode",
    "enable_lite_docstrings",
    "enable_dashboard_screenshot",
)


def maybe_auto_enable_beta_master(config: dict[str, Any]) -> None:
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
    truthy = [key for key in _DEV_ADDON_BETA_KEYS if config.get(key) is True]
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
    enable_tool_security_policies = False  # default
    read_only_mode = False  # default (discussion #1569 — non-beta, off by default)
    enable_yaml_config_editing = False  # default
    yaml_config_in_config = False  # presence flag
    # Per-key sub-gates of enable_yaml_config_editing (dev-addon schema
    # only). Each follows the same presence-tracked pattern as the
    # parent so stable installs (key absent) fall through to the
    # standalone file/default origin chain instead of being pinned to
    # origin='addon'.
    enable_yaml_packages_automation = False  # default
    yaml_packages_automation_in_config = False  # presence flag
    enable_yaml_packages_script = False  # default
    yaml_packages_script_in_config = False  # presence flag
    enable_yaml_packages_scene = False  # default
    yaml_packages_scene_in_config = False  # presence flag
    enable_filesystem_tools = False  # default
    filesystem_tools_in_config = False  # presence flag
    enable_custom_component_integration = False  # default
    custom_component_in_config = False  # presence flag
    enable_code_mode = False  # default
    code_mode_in_config = False  # presence flag
    enable_dashboard_screenshot = False  # default
    dashboard_screenshot_in_config = False  # presence flag
    enable_lite_docstrings = False  # default
    lite_docstrings_in_config = False  # presence flag
    enable_mandatory_bps = True  # default (issue #1182 — on by default, non-beta)
    # Master beta toggle: present only in the dev addon's schema.
    # Default to False (stable behaviour); when
    # the dev schema-default merges in, ``beta_master_in_config``
    # flips to True and the actual value comes from the addon options.
    beta_master_in_config = False
    enable_beta_features = False
    enable_auto_backup = (
        True  # default (#1288 — on by default; opt out via ENABLE_AUTO_BACKUP=false)
    )
    auto_backup_throttle_minutes = 0  # default — every write
    auto_backup_retain_per_entity = 100  # default
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
            enable_tool_search = (
                raw_tool_search if isinstance(raw_tool_search, bool) else False
            )
            raw_tool_security_policies = config.get(
                "enable_tool_security_policies", False
            )
            enable_tool_security_policies = (
                raw_tool_security_policies
                if isinstance(raw_tool_security_policies, bool)
                else False
            )
            read_only_mode = resolve_bool_option(config, "read_only_mode", False)
            # Beta sub-flag presence tracking. On stable-addon, the 5
            # beta keys are NOT in config.yaml
            # schema — options.json carries none of them. If we wrote
            # ENABLE_YAML_CONFIG_EDITING=false (etc.) unconditionally,
            # get_feature_flag_origin would see env-var-set + in_addon
            # → origin='addon' → UI labels editable. The user toggles,
            # save POSTs to Supervisor, schema rejects (key not in
            # stable schema). Track presence and skip the env write
            # below when absent so stable falls through to the
            # standalone file/default origin chain.
            yaml_config_in_config = "enable_yaml_config_editing" in config
            raw_yaml_config = config.get("enable_yaml_config_editing", False)
            enable_yaml_config_editing = (
                raw_yaml_config if isinstance(raw_yaml_config, bool) else False
            )
            yaml_packages_automation_in_config = (
                "enable_yaml_packages_automation" in config
            )
            raw_yaml_pkg_automation = config.get(
                "enable_yaml_packages_automation", False
            )
            enable_yaml_packages_automation = (
                raw_yaml_pkg_automation
                if isinstance(raw_yaml_pkg_automation, bool)
                else False
            )
            yaml_packages_script_in_config = "enable_yaml_packages_script" in config
            raw_yaml_pkg_script = config.get("enable_yaml_packages_script", False)
            enable_yaml_packages_script = (
                raw_yaml_pkg_script if isinstance(raw_yaml_pkg_script, bool) else False
            )
            yaml_packages_scene_in_config = "enable_yaml_packages_scene" in config
            raw_yaml_pkg_scene = config.get("enable_yaml_packages_scene", False)
            enable_yaml_packages_scene = (
                raw_yaml_pkg_scene if isinstance(raw_yaml_pkg_scene, bool) else False
            )
            filesystem_tools_in_config = "enable_filesystem_tools" in config
            raw_filesystem_tools = config.get("enable_filesystem_tools", False)
            enable_filesystem_tools = (
                raw_filesystem_tools
                if isinstance(raw_filesystem_tools, bool)
                else False
            )
            dashboard_screenshot_in_config = "enable_dashboard_screenshot" in config
            raw_dashboard_screenshot = config.get("enable_dashboard_screenshot", False)
            enable_dashboard_screenshot = (
                raw_dashboard_screenshot
                if isinstance(raw_dashboard_screenshot, bool)
                else False
            )
            custom_component_in_config = "enable_custom_component_integration" in config
            raw_custom_component = config.get(
                "enable_custom_component_integration", False
            )
            enable_custom_component_integration = (
                raw_custom_component
                if isinstance(raw_custom_component, bool)
                else False
            )
            code_mode_in_config = "enable_code_mode" in config
            raw_code_mode = config.get("enable_code_mode", False)
            enable_code_mode = (
                raw_code_mode if isinstance(raw_code_mode, bool) else False
            )
            lite_docstrings_in_config = "enable_lite_docstrings" in config
            raw_lite_docstrings = config.get("enable_lite_docstrings", False)
            enable_lite_docstrings = (
                raw_lite_docstrings if isinstance(raw_lite_docstrings, bool) else False
            )
            raw_mandatory_bps = config.get("enable_mandatory_bps", True)
            if isinstance(raw_mandatory_bps, bool):
                enable_mandatory_bps = raw_mandatory_bps
            else:
                log_error(
                    "enable_mandatory_bps must be bool, got "
                    f"{type(raw_mandatory_bps).__name__}={raw_mandatory_bps!r}; "
                    "using default True"
                )
                enable_mandatory_bps = True
            # Master beta toggle is present in the dev-addon schema.
            # Track presence separately so stable
            # add-on installs (where the key is absent from options.json)
            # do NOT get an explicit ENABLE_BETA_FEATURES=false env var
            # — that would force the web UI to render the master as
            # ``origin=env, locked`` and the standalone user could not
            # toggle it.
            beta_master_in_config = "enable_beta_features" in config
            raw_beta_master = config.get("enable_beta_features", False)
            enable_beta_features = (
                raw_beta_master if isinstance(raw_beta_master, bool) else False
            )
            raw_auto_backup = config.get("enable_auto_backup", True)
            enable_auto_backup = (
                raw_auto_backup if isinstance(raw_auto_backup, bool) else False
            )
            raw_throttle = config.get("auto_backup_throttle_minutes", 0)
            auto_backup_throttle_minutes = (
                raw_throttle if isinstance(raw_throttle, int) else 0
            )
            raw_retain = config.get("auto_backup_retain_per_entity", 100)
            auto_backup_retain_per_entity = (
                raw_retain if isinstance(raw_retain, int) else 100
            )
            raw_max_results = config.get("tool_search_max_results", 5)
            tool_search_max_results = (
                raw_max_results if isinstance(raw_max_results, int) else 5
            )
            raw_disabled = config.get("disabled_tools", "")
            disabled_tools_raw = raw_disabled if isinstance(raw_disabled, str) else ""
            raw_pinned = config.get("pinned_tools", "")
            pinned_tools_raw = raw_pinned if isinstance(raw_pinned, str) else ""
            verify_ssl = resolve_bool_option(config, "verify_ssl", True)
            advanced_debug_logging = resolve_bool_option(
                config, "advanced_debug_logging", False
            )
        except Exception as e:
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
    os.environ["ENABLE_TOOL_SECURITY_POLICIES"] = str(
        enable_tool_security_policies
    ).lower()
    # READ_ONLY_MODE is non-beta and in BOTH addon schemas, so it is
    # written unconditionally (like ENABLE_MANDATORY_BPS below).
    os.environ["READ_ONLY_MODE"] = str(read_only_mode).lower()
    # ENABLE_MANDATORY_BPS is non-beta and default-ON, so it is written
    # unconditionally (like the stable core settings above) — never
    # presence-gated or beta-master-gated like the beta sub-flags below.
    os.environ["ENABLE_MANDATORY_BPS"] = str(enable_mandatory_bps).lower()
    # Beta sub-flags: only write env vars when the key is actually in
    # the addon's options.json. On stable addon,
    # none of these keys are in schema, so config.get(...) returned
    # the default False — but explicitly writing the env would mark
    # the field as origin='addon' (Supervisor-managed) in the web UI,
    # and Supervisor would reject the eventual save because the key
    # is not in stable's schema. Skip the write so the standalone
    # file/default origin chain applies.
    if yaml_config_in_config:
        os.environ["ENABLE_YAML_CONFIG_EDITING"] = str(
            enable_yaml_config_editing
        ).lower()
    if yaml_packages_automation_in_config:
        os.environ["ENABLE_YAML_PACKAGES_AUTOMATION"] = str(
            enable_yaml_packages_automation
        ).lower()
    if yaml_packages_script_in_config:
        os.environ["ENABLE_YAML_PACKAGES_SCRIPT"] = str(
            enable_yaml_packages_script
        ).lower()
    if yaml_packages_scene_in_config:
        os.environ["ENABLE_YAML_PACKAGES_SCENE"] = str(
            enable_yaml_packages_scene
        ).lower()
    if filesystem_tools_in_config:
        os.environ["HAMCP_ENABLE_FILESYSTEM_TOOLS"] = str(
            enable_filesystem_tools
        ).lower()
    if custom_component_in_config:
        os.environ["HAMCP_ENABLE_CUSTOM_COMPONENT_INTEGRATION"] = str(
            enable_custom_component_integration
        ).lower()
    if dashboard_screenshot_in_config:
        os.environ["HAMCP_ENABLE_DASHBOARD_SCREENSHOT"] = str(
            enable_dashboard_screenshot
        ).lower()
    if code_mode_in_config:
        os.environ["ENABLE_CODE_MODE"] = str(enable_code_mode).lower()
    if lite_docstrings_in_config:
        os.environ["ENABLE_LITE_DOCSTRINGS"] = str(enable_lite_docstrings).lower()
    # Dev-upgrade silent-disable warning: if the master is in
    # options.json and is False, but any sub-flag is truthy, the
    # runtime gate will force the sub-flag off. Log loudly so an
    # operator who had beta tools on before the master-in-schema
    # rollout, then toggled the master off after the update, can see
    # why their tools went away.
    if beta_master_in_config and enable_beta_features is False:
        gated_off = [
            name
            for name, present, value in (
                (
                    "enable_yaml_config_editing",
                    yaml_config_in_config,
                    enable_yaml_config_editing,
                ),
                (
                    "enable_filesystem_tools",
                    filesystem_tools_in_config,
                    enable_filesystem_tools,
                ),
                (
                    "enable_custom_component_integration",
                    custom_component_in_config,
                    enable_custom_component_integration,
                ),
                (
                    "enable_dashboard_screenshot",
                    dashboard_screenshot_in_config,
                    enable_dashboard_screenshot,
                ),
                ("enable_code_mode", code_mode_in_config, enable_code_mode),
                (
                    "enable_lite_docstrings",
                    lite_docstrings_in_config,
                    enable_lite_docstrings,
                ),
            )
            if present and value
        ]
        if gated_off:
            log_info(
                "Master beta toggle is OFF but these sub-flags are set "
                f"to true in options.json — they will be force-disabled "
                f"at runtime by the master gate: {', '.join(gated_off)}. "
                "Re-enable the master toggle in the addon Configuration "
                "tab (or the web settings UI) to use them."
            )
    # Master beta toggle: write env var only when the key exists in
    # the addon's options.json. Dev addon's schema declares it (so
    # the key is always present, value follows the user's toggle).
    # Stable addon's schema does not declare it (so the key is absent
    # and the standalone web-UI master path remains the gate).
    if beta_master_in_config:
        os.environ["ENABLE_BETA_FEATURES"] = str(enable_beta_features).lower()
    else:
        # Legacy safety net: dev-addon installs that pre-date the
        # master-in-schema rollout don't carry the key yet, but their
        # truthy sub-flag presence still implies the user wants beta
        # tools on. Keep the auto-enable as a one-cycle bridge until
        # Supervisor merges the new schema default into options.json.
        maybe_auto_enable_beta_master(config)
    os.environ["ENABLE_AUTO_BACKUP"] = str(enable_auto_backup).lower()
    os.environ["AUTO_BACKUP_THROTTLE_MINUTES"] = str(auto_backup_throttle_minutes)
    os.environ["AUTO_BACKUP_RETAIN_PER_ENTITY"] = str(auto_backup_retain_per_entity)
    # Persist saved custom tools across addon restarts. /data is the
    # per-addon writable directory mapped by Supervisor and survives
    # add-on updates (but not uninstall/reinstall — users should copy
    # this file out before reinstalling if they want to migrate).
    # Setting this unconditionally is safe: on the stable add-on the
    # tool isn't registered anyway, so the file is never read or
    # written. This path is hardcoded in add-on mode: it is not in the
    # add-on config.yaml schema, so add-on operators have no surface to
    # change it — and /data is the only location that survives add-on
    # updates anyway.
    os.environ.setdefault("CODE_MODE_SAVED_TOOLS_PATH", "/data/saved_tools.json")
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
        _log_startup_version,
        mcp,
        register_browser_landing,
    )
    from ha_mcp.settings_ui import register_settings_routes

    # Log the ha-mcp version + a self-update banner when a newer release is
    # available. In the add-on that comes from the Supervisor add-on store, not
    # PyPI (see update_check._resolve_update_info's is_running_in_addon branch).
    # The addon runs its own startup here (it doesn't go through
    # __main__.main_web), so without this the ha-mcp banner never reaches the
    # addon logs — only FastMCP's own banner does (via run_async). Mirrors how
    # FastMCP surfaces its update notice in these same startup logs.
    _log_startup_version()

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
    register_settings_routes(
        server_instance.mcp, server_instance, secret_path=secret_path
    )
    logging.getLogger("mcp.server.streamable_http").addFilter(
        StatelessSessionLogFilter()
    )

    # The addon normally binds to 0.0.0.0 so HA Supervisor ingress can
    # reach it inside the container; MCP_HOST override is provided for
    # parity with the standard CLI entry points (see issue #1434).
    bind_host = os.getenv("MCP_HOST", "0.0.0.0")

    try:
        log_info("Starting MCP server...")
        if bind_host != "0.0.0.0":
            log_info(f"Bind host overridden via MCP_HOST: {bind_host}")
        mcp.run(
            transport="http",
            host=bind_host,
            port=port,
            path=secret_path,
            stateless_http=True,
            uvicorn_config={"log_config": _get_timestamped_uvicorn_log_config()},
        )
    except KeyboardInterrupt:
        log_info("Interrupted, exiting")
        return 0
    except BaseException as e:
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


if __name__ == "__main__":
    sys.exit(main())
