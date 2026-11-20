"""Read and validate the app's options for ``start.py``.

Reads the options file and loads the component's allowlist module from the
bundled component source; ``start.py`` passes in both locations and its log
functions.
"""

from __future__ import annotations

import importlib.util
import json
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from types import ModuleType


def read_app_options(
    config_file: Path, log_error: Callable[[str], None]
) -> dict | None:
    """Read the app (add-on) options from ``config_file`` (``/data/options.json``).

    Returns a dict of the parsed option values (defaults filled in for anything
    absent), or None when the file is present but unreadable/corrupt — a
    fail-closed signal the caller turns into a non-zero exit.
    """
    # Read config. Supervisor always writes /data/options.json, so the outer
    # existence check stays best-effort for the (unreachable) absent-file case.
    # A present-but-unreadable/corrupt file, however, is FATAL (fail closed
    # below): we can't tell whether the user enabled OAuth, and silently
    # falling back to the unauthenticated defaults would betray that intent.
    remote_url = ""
    mcp_server_url = ""
    mcp_port = 9583
    enable_oauth = False
    oauth_client_id = ""
    oauth_client_secret = ""
    oauth_mode_option = ""
    regenerate_oauth_creds = False
    debug_logging = False
    oauth_redirect_allowlist = None
    oauth_allow_any_callback = False
    config: dict = {}

    if config_file.exists():
        try:
            config = json.load(config_file.open())
            remote_url = config.get("remote_url", "")
            mcp_server_url = config.get("mcp_server_url", "")
            mcp_port = config.get("mcp_port", 9583)
            enable_oauth = bool(config.get("enable_oauth", False))
            oauth_client_id = config.get("oauth_client_id", "")
            oauth_client_secret = config.get("oauth_client_secret", "")
            # Unset by default in config.yaml options (no default value), so an
            # upgrader inherits nothing; start.py resolves the mode.
            oauth_mode_option = config.get("oauth_mode", "")
            regenerate_oauth_creds = bool(config.get("regenerate_oauth_creds", False))
            debug_logging = bool(config.get("debug_logging", False))
            oauth_redirect_allowlist = config.get("oauth_redirect_allowlist")
            oauth_allow_any_callback = bool(config.get("oauth_allow_any_callback"))
        except (OSError, json.JSONDecodeError) as e:
            # Fail closed: the app config is present but unreadable/corrupt,
            # so the user's intent is unknown — including whether OAuth should
            # be enforced. Continuing would leave enable_oauth at its False
            # default, silently serving an unauthenticated proxy (URL-secrecy
            # only) to a user who turned OAuth ON. Refuse to start instead.
            log_error("")
            log_error("=" * 70)
            log_error(
                f"  Could not read the app configuration ({type(e).__name__}): {e}"
            )
            log_error("")
            log_error("  The app options file could not be parsed, so your")
            log_error("  configured intent is unknown — including whether OAuth")
            log_error("  should be enforced. Starting now could serve the webhook")
            log_error("  WITHOUT the authentication you asked for, so the app is")
            log_error("  refusing to start.")
            log_error("")
            log_error("  Restart the app to recover: the Supervisor rewrites")
            log_error("  this file from your saved configuration on every start,")
            log_error("  so a restart normally heals a corrupted copy. If the")
            log_error("  error persists across restarts, re-save the app's")
            log_error("  Configuration tab and check the system's storage.")
            log_error("=" * 70)
            log_error("")
            return None

    return {
        "remote_url": remote_url,
        "mcp_server_url": mcp_server_url,
        "mcp_port": mcp_port,
        "enable_oauth": enable_oauth,
        "oauth_client_id": oauth_client_id,
        "oauth_client_secret": oauth_client_secret,
        "oauth_mode_option": oauth_mode_option,
        "regenerate_oauth_creds": regenerate_oauth_creds,
        "debug_logging": debug_logging,
        "oauth_redirect_allowlist": oauth_redirect_allowlist,
        "oauth_allow_any_callback": oauth_allow_any_callback,
        "config": config,
    }


class CallbackOptions(NamedTuple):
    """None-mode OAuth callback options, validated for the proxy config."""

    # Normalized list; None follows the component's built-in list.
    allowlist: list[str] | None
    allow_any: bool
    # Entries the user configured, before any were dropped.
    configured: int = 0


def _load_allowlist_module(component_src: Path) -> ModuleType:
    """The component's standard-library-only allowlist module, from the
    bundled source: this process runs outside Home Assistant."""
    path = component_src / "oauth_redirect_allowlist.py"
    spec = importlib.util.spec_from_file_location("oauth_redirect_allowlist", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolve_callback_options(
    options: dict,
    component_src: Path,
    log_error: Callable[[str], None],
    log_warning: Callable[[str], None],
) -> CallbackOptions:
    """Validate 'Allowed OAuth callback URLs', dropping and naming unusable
    entries and any beyond the entry limit."""
    raw = options["oauth_redirect_allowlist"]
    allow_any = bool(options["oauth_allow_any_callback"])
    if options["enable_oauth"] and (raw is not None or allow_any):
        log_warning(
            "'Allowed OAuth callback URLs' and 'Allow any OAuth callback URL' "
            "apply only while Enable OAuth is off; they have no effect now."
        )
    if raw is None:
        return CallbackOptions(None, allow_any)
    allowlist = _load_allowlist_module(component_src)
    limit: int = allowlist.MAX_OAUTH_CALLBACKS
    values = raw if isinstance(raw, list) else [raw]
    entries, invalid = allowlist.normalize_allowlist(values)
    if invalid:
        log_error(
            f"Ignoring 'Allowed OAuth callback URLs' entries {invalid}: each must "
            "be https or http on a loopback address such as localhost or "
            "127.0.0.1, with no username/password and no #fragment, at most "
            f"{allowlist.MAX_OAUTH_CALLBACK_LENGTH} characters."
        )
    if beyond := entries[limit:]:
        log_error(
            f"Ignoring 'Allowed OAuth callback URLs' entries {beyond}: they are "
            f"beyond the limit of {limit} entries."
        )
    return CallbackOptions(entries[:limit], allow_any, len(values))


def callback_config(callbacks: CallbackOptions) -> dict:
    """The proxy-config keys for the callback options; empty when unset."""
    config: dict = {}
    if callbacks.allowlist is not None:
        config["oauth_redirect_allowlist"] = callbacks.allowlist
    if callbacks.allow_any:
        config["oauth_allow_any_callback"] = True
    return config


def callback_summary(callbacks: CallbackOptions) -> str:
    """Which callbacks may complete OAuth sign-in, for the startup log."""
    if callbacks.allow_any:
        return "any (Allow any OAuth callback URL is on)"
    if callbacks.allowlist is None:
        return "the built-in list"
    if not callbacks.allowlist and callbacks.configured:
        what = (
            "the 1 configured entry was"
            if callbacks.configured == 1
            else f"all {callbacks.configured} configured entries were"
        )
        return f"none ({what} ignored, so every sign-in is refused)"
    if not callbacks.allowlist:
        return "none (the configured list is empty, so every sign-in is refused)"
    return f"{len(callbacks.allowlist)} configured callback(s)"
