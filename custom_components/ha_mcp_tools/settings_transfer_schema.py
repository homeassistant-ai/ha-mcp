"""Versioned, portable fields of the two HA-MCP config entries."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol

from . import _normalize_extra_dir, _normalize_extra_yaml_keys
from . import const as c
from .oauth_redirect_allowlist import normalize_allowlist

FORMAT = "ha-mcp-settings"
VERSION = 1
MAX_BYTES = 4 * 1024 * 1024


class InvalidBackup(ValueError):
    """A stable error code that may be returned without revealing input values."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _port(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise vol.Invalid("invalid port")
    return value


def _external_url(value: str) -> str:
    try:
        url = urlsplit(value)
        _ = url.port  # Forces validation of malformed or out-of-range ports.
        if not value or (
            url.scheme in {"http", "https"}
            and url.hostname
            and url.username is None
            and not url.query
            and not url.fragment
            and not any(ch.isspace() or ord(ch) < 32 for ch in value)
        ):
            return value
    except ValueError:
        pass
    raise vol.Invalid("invalid external URL")


def _callbacks(values: list[str]) -> list[str]:
    entries, rejected = normalize_allowlist(values)
    if rejected or len(entries) > c.MAX_OAUTH_CALLBACKS:
        raise vol.Invalid("invalid OAuth callbacks")
    return entries


def _path_value(value: str) -> str:
    # The Configure form supports nested paths, with or without a leading
    # slash and with a trailing slash. Keep the operator's exact URL.
    if (
        not value.strip("/")
        or value.startswith("//")
        or any(ch in "\\?#%" or ch.isspace() or ord(ch) < 32 for ch in value)
        or any(segment in (".", "..") for segment in value.split("/"))
    ):
        raise vol.Invalid("invalid connection path")
    return value


_secret = vol.All(str, vol.Length(min=1))
_signing_key = vol.All(str, vol.Match(r"^[a-fA-F0-9]{64}\Z"))
_webhook = vol.All(str, _path_value, vol.Match(r"^[^/]+\Z"))
_path = vol.All(str, _path_value, vol.Match(r"^/"))

SERVER_DATA = {
    vol.Required(c.DATA_WEBHOOK_ID): _webhook,
    vol.Required(c.DATA_SECRET_PATH): _path,
    c.DATA_ACCESS_ENABLED: bool,
    c.DATA_OAUTH_CLIENT_ID: _secret,
    c.DATA_OAUTH_CLIENT_SECRET: _secret,
    c.DATA_OAUTH_SIGNING_KEY: _signing_key,
    c.DATA_DCR_SIGNING_KEY: _signing_key,
}

SERVER_OPTIONS = {
    c.OPT_SERVER_PORT: _port,
    c.OPT_BIND_HOST: vol.In([c.BIND_HOST_ALL, c.BIND_HOST_LOOPBACK]),
    c.OPT_ENABLE_WEBHOOK: bool,
    c.OPT_WEBHOOK_AUTH: vol.In(
        [c.WEBHOOK_AUTH_NONE, c.WEBHOOK_AUTH_HA, c.WEBHOOK_AUTH_LEGACY]
    ),
    c.OPT_EXTERNAL_URL: vol.All(str, _external_url),
    c.OPT_WEBHOOK_ID_OVERRIDE: vol.Any("", _webhook),
    c.OPT_SECRET_PATH_OVERRIDE: vol.Any("", vol.All(str, _path_value)),
    c.OPT_ENABLE_STARTUP_NOTIFICATION: bool,
    c.OPT_ENABLE_SIDEBAR_PANEL: bool,
    c.OPT_ACCESS_OFF_AFTER_RESTART: bool,
    c.OPT_ENABLE_LLM_API: bool,
    c.OPT_LLM_API_EXPOSURE: vol.In(
        [c.EXPOSURE_TOOL_SEARCH, c.EXPOSURE_FULL, c.EXPOSURE_BOTH]
    ),
    c.OPT_OAUTH_REDIRECT_ALLOWLIST: vol.All([str], _callbacks),
    c.OPT_OAUTH_ALLOW_ANY_CALLBACK: bool,
}

# Pending one-shot changes must not override the imported credentials on reload.
TRANSIENT_OPTIONS = {
    c.OPT_REGENERATE_SECRETS,
    c.OPT_OAUTH_REGENERATE,
    c.OPT_OAUTH_CLIENT_ID,
    c.OPT_OAUTH_CLIENT_SECRET,
}

_ENVELOPE = vol.Schema(
    {
        vol.Required("format"): FORMAT,
        vol.Required("version"): int,
        vol.Required("contains_credentials"): True,
        vol.Required("entries"): {
            vol.Required(c.ENTRY_TYPE_SERVER): {
                vol.Required("data"): SERVER_DATA,
                vol.Required("options"): SERVER_OPTIONS,
            },
            c.ENTRY_TYPE_TOOLS: {
                vol.Required("allowed_dirs"): [str],
                vol.Required("extra_yaml_keys"): [str],
                "disabled": bool,
            },
        },
        vol.Required("server_settings"): dict,
    }
)


def portable_server(entry: Any) -> dict[str, Any]:
    """Project onto the allowlists before a secret-bearing export leaves HA."""
    return {
        "data": {k: v for k, v in entry.data.items() if k in SERVER_DATA},
        "options": {k: v for k, v in entry.options.items() if k in SERVER_OPTIONS},
    }


def validate(bundle: Any, config_dir: Path) -> dict[str, Any]:
    """Validate the complete entry payload before writing either entry or store."""
    try:
        checked: dict[str, Any] = _ENVELOPE(bundle)
    except (vol.Invalid, TypeError, ValueError) as err:
        raise InvalidBackup("invalid_backup") from err
    if type(checked["version"]) is not int or checked["version"] != VERSION:
        raise InvalidBackup("incompatible_version")
    if checked["contains_credentials"] is not True:
        raise InvalidBackup("invalid_backup")
    server = checked["entries"][c.ENTRY_TYPE_SERVER]
    legacy_fields = {
        c.DATA_OAUTH_CLIENT_ID,
        c.DATA_OAUTH_CLIENT_SECRET,
        c.DATA_OAUTH_SIGNING_KEY,
    }
    has_legacy = server["options"].get(c.OPT_WEBHOOK_AUTH) == c.WEBHOOK_AUTH_LEGACY
    if has_legacy or legacy_fields.intersection(server["data"]):
        if not legacy_fields.issubset(server["data"]):
            raise InvalidBackup("invalid_backup")
    if tools := checked["entries"].get(c.ENTRY_TYPE_TOOLS):
        paths = [
            _normalize_extra_dir(item, config_dir) for item in tools["allowed_dirs"]
        ]
        if None in paths:
            raise InvalidBackup("invalid_backup")
        keys, dropped = _normalize_extra_yaml_keys(tools["extra_yaml_keys"])
        if dropped:
            raise InvalidBackup("invalid_backup")
        tools["allowed_dirs"] = list(dict.fromkeys(paths))
        tools["extra_yaml_keys"] = keys
    return checked


def restored_entry(
    entry: Any,
    server: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Replace portable values, retaining destination-only credentials and options."""
    data = {k: v for k, v in entry.data.items() if k not in SERVER_DATA}
    options = {
        k: v
        for k, v in entry.options.items()
        if k not in SERVER_OPTIONS and k not in TRANSIENT_OPTIONS
    }
    return data | server["data"], options | server["options"]
