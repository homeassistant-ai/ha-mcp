"""Read and validate the app's options for ``start.py``.

Pure functions: ``start.py`` passes in every path and its log function, so
this module builds no paths of its own.
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
    """Read the add-on options from ``config_file`` (``/data/options.json``).

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
            # upgrader inherits nothing — the mode is resolved below.
            oauth_mode_option = config.get("oauth_mode", "")
            regenerate_oauth_creds = bool(config.get("regenerate_oauth_creds", False))
            debug_logging = bool(config.get("debug_logging", False))
            oauth_redirect_allowlist = config.get("oauth_redirect_allowlist")
            oauth_allow_any_callback = bool(config.get("oauth_allow_any_callback"))
        except (OSError, json.JSONDecodeError) as e:
            # Fail closed: the add-on config is present but unreadable/corrupt,
            # so the user's intent is unknown — including whether OAuth should
            # be enforced. Continuing would leave enable_oauth at its False
            # default, silently serving an unauthenticated proxy (URL-secrecy
            # only) to a user who turned OAuth ON. Refuse to start instead.
            log_error("")
            log_error("=" * 70)
            log_error(
                f"  Could not read the add-on configuration ({type(e).__name__}): {e}"
            )
            log_error("")
            log_error("  The add-on options file could not be parsed, so your")
            log_error("  configured intent is unknown — including whether OAuth")
            log_error("  should be enforced. Starting now could serve the webhook")
            log_error("  WITHOUT the authentication you asked for, so the add-on is")
            log_error("  refusing to start.")
            log_error("")
            log_error("  Restart the add-on to recover: the Supervisor rewrites")
            log_error("  this file from your saved configuration on every start,")
            log_error("  so a restart normally heals a corrupted copy. If the")
            log_error("  error persists across restarts, re-save the add-on's")
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
    options: dict, component_src: Path, log_error: Callable[[str], None]
) -> CallbackOptions:
    """Validate 'Allowed OAuth callback URLs', dropping and naming unusable
    entries and any beyond the entry limit."""
    raw = options["oauth_redirect_allowlist"]
    allow_any = bool(options["oauth_allow_any_callback"])
    if raw is None:
        return CallbackOptions(None, allow_any)
    allowlist = _load_allowlist_module(component_src)
    limit: int = allowlist.MAX_OAUTH_CALLBACKS
    entries, invalid = allowlist.normalize_allowlist(
        raw if isinstance(raw, list) else [raw]
    )
    if dropped := invalid + entries[limit:]:
        log_error(
            f"Ignoring 'Allowed OAuth callback URLs' entries {dropped}: each must "
            "be https (or http on localhost/127.0.0.1), with no username/password "
            f"and no #fragment, at most {allowlist.MAX_OAUTH_CALLBACK_LENGTH} "
            f"characters; at most {limit} entries."
        )
    return CallbackOptions(entries[:limit], allow_any)


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
    return f"{len(callbacks.allowlist)} configured callback(s)"
