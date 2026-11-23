"""Versioned, portable fields of the two HA-MCP config entries."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

import voluptuous as vol

from . import _normalize_extra_dir
from . import const as c
from .extra_yaml_keys import _normalize_extra_yaml_keys
from .oauth_redirect_allowlist import (
    MAX_OAUTH_CALLBACKS,
    OPT_OAUTH_REDIRECT_ALLOWLIST,
    normalize_allowlist,
)

FORMAT = "ha-mcp-settings"
VERSION = 1
MAX_BYTES = 4 * 1024 * 1024


class RefusalCode(StrEnum):
    """Why a transfer was refused. Each code has a ``transfer.*`` panel message."""

    INVALID_BACKUP = "invalid_backup"
    INCOMPATIBLE_VERSION = "incompatible_version"
    INVALID_SERVER_SETTINGS = "invalid_server_settings"
    DECISION_PIN_REQUIRED = "decision_pin_required"
    RESTORE_FAILED = "restore_failed"
    SERVER_UNAVAILABLE = "server_unavailable"
    WEBHOOK_IN_USE = "webhook_in_use"
    EXTRA_TOOLS_ENTRY = "extra_tools_entry"
    TOOLS_MUST_BE_ENABLED = "tools_must_be_enabled"
    TOOLS_MUST_BE_DISABLED = "tools_must_be_disabled"
    TOOLS_ENTRY_BUSY = "tools_entry_busy"
    TOOLS_ENTRY_UNAVAILABLE = "tools_entry_unavailable"

    @property
    def http_status(self) -> int:
        """409 when this installation's state blocks the transfer, else 400."""
        return 409 if self in _CONFLICTS else 400


_CONFLICTS = frozenset(
    {
        RefusalCode.SERVER_UNAVAILABLE,
        RefusalCode.WEBHOOK_IN_USE,
        RefusalCode.EXTRA_TOOLS_ENTRY,
        RefusalCode.TOOLS_MUST_BE_ENABLED,
        RefusalCode.TOOLS_MUST_BE_DISABLED,
        RefusalCode.TOOLS_ENTRY_BUSY,
        RefusalCode.TOOLS_ENTRY_UNAVAILABLE,
    }
)


class TransferRefused(ValueError):
    """A refusal whose code may be returned without revealing input values."""

    def __init__(self, code: RefusalCode) -> None:
        super().__init__(code.value)
        self.code = code


def _port(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise vol.Invalid("invalid port")
    return value


def _external_url(value: str) -> str:
    # Configure stores any single-line text here, stripped of surrounding
    # whitespace and a trailing slash; accept every value it can save.
    if value != value.strip().rstrip("/") or any(
        ord(ch) < 32 or ord(ch) == 127 for ch in value
    ):
        raise vol.Invalid("invalid external URL")
    return value


def _callbacks(values: list[str]) -> list[str]:
    entries, rejected = normalize_allowlist(values)
    if rejected or len(entries) > MAX_OAUTH_CALLBACKS:
        raise vol.Invalid("invalid OAuth callbacks")
    return entries


def _path_value(value: str) -> str:
    # What Configure saves: any path, "/" included, without a query, fragment,
    # percent escape or whitespace. Keep the operator's exact URL.
    if not value or any(ch in "?#%" or ch.isspace() or ord(ch) < 32 for ch in value):
        raise vol.Invalid("invalid connection path")
    return value


_secret = vol.All(str, vol.Length(min=1))
_signing_key = vol.All(str, vol.Match(r"^[a-fA-F0-9]{64}\Z"))
_webhook = vol.All(str, _path_value, vol.Match(r"^[^/]+\Z"))
_path = vol.All(str, _path_value, vol.Match(r"^/"))

# The Access switch (DATA_ACCESS_ENABLED) is the destination's own state.
SERVER_DATA = {
    vol.Required(c.DATA_WEBHOOK_ID): _webhook,
    vol.Required(c.DATA_SECRET_PATH): _path,
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
    OPT_OAUTH_REDIRECT_ALLOWLIST: vol.All([str], _callbacks),
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
        vol.Required("contains_credentials"): bool,
        vol.Required("entries"): {
            # Only an export from the HA-MCP Server entry carries the
            # connection; another installation's export moves the settings.
            c.ENTRY_TYPE_SERVER: {
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


def _check_legacy_credentials(server: Mapping[str, Any]) -> None:
    legacy_fields = {
        c.DATA_OAUTH_CLIENT_ID,
        c.DATA_OAUTH_CLIENT_SECRET,
        c.DATA_OAUTH_SIGNING_KEY,
    }
    has_legacy = server["options"].get(c.OPT_WEBHOOK_AUTH) == c.WEBHOOK_AUTH_LEGACY
    if has_legacy or legacy_fields.intersection(server["data"]):
        if not legacy_fields.issubset(server["data"]):
            raise TransferRefused(RefusalCode.INVALID_BACKUP)


def validate(bundle: Any, config_dir: Path, *, stored: bool = False) -> dict[str, Any]:
    """Validate the complete entry payload before writing either entry or store.

    An export (``stored``) drops the directories and YAML keys that the File &
    YAML Tools startup would also drop; an upload containing one is refused.
    """
    # Another version may have another shape, so check it before the v1 schema.
    if not isinstance(bundle, dict) or bundle.get("format") != FORMAT:
        raise TransferRefused(RefusalCode.INVALID_BACKUP)
    if type(bundle.get("version")) is not int or bundle["version"] != VERSION:
        raise TransferRefused(RefusalCode.INCOMPATIBLE_VERSION)
    try:
        checked: dict[str, Any] = _ENVELOPE(bundle)
    except (vol.Invalid, TypeError, ValueError) as err:
        raise TransferRefused(RefusalCode.INVALID_BACKUP) from err
    server = checked["entries"].get(c.ENTRY_TYPE_SERVER)
    if checked["contains_credentials"] is not (server is not None):
        raise TransferRefused(RefusalCode.INVALID_BACKUP)
    if server is not None:
        _check_legacy_credentials(server)
    if tools := checked["entries"].get(c.ENTRY_TYPE_TOOLS):
        paths = [
            _normalize_extra_dir(item, config_dir) for item in tools["allowed_dirs"]
        ]
        keys, dropped = _normalize_extra_yaml_keys(tools["extra_yaml_keys"])
        if not stored and (None in paths or dropped):
            raise TransferRefused(RefusalCode.INVALID_BACKUP)
        tools["allowed_dirs"] = list(dict.fromkeys(p for p in paths if p is not None))
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
