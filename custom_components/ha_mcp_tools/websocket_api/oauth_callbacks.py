"""OAuth callback allowlist commands: oauth_callbacks, oauth_callbacks_update.

The settings panel edits the none-mode callback allowlist (#2427) through these.
The list is an option on the server entry, read per request by the
auto-approve ``/authorize``, so a save applies to the next sign-in without a
reload — which matters because the save arrives through the in-process server
a reload would stop.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant

from ..const import (
    DEFAULT_OAUTH_REDIRECT_ALLOWLIST,
    OPT_ENABLE_WEBHOOK,
    OPT_OAUTH_REDIRECT_ALLOWLIST,
    OPT_WEBHOOK_AUTH,
    WEBHOOK_AUTH_NONE,
)
from ..oauth_redirect_allowlist import (
    effective_allowlist,
    normalize_allowlist,
    stored_allowlist,
)
from .constants import (
    MAX_OAUTH_CALLBACK_LENGTH,
    MAX_OAUTH_CALLBACKS,
    WS_OAUTH_CALLBACKS,
    WS_OAUTH_CALLBACKS_UPDATE,
)
from .system import _find_server_config_entry


def _oauth_callbacks_schema() -> dict[Any, Any]:
    return {vol.Required("type"): WS_OAUTH_CALLBACKS}


def _oauth_callbacks_update_schema() -> dict[Any, Any]:
    # ``reset`` restores the shipped default by removing the saved list.
    return {
        vol.Required("type"): WS_OAUTH_CALLBACKS_UPDATE,
        vol.Exclusive("allowlist", "change"): vol.All(
            [vol.All(str, vol.Length(max=MAX_OAUTH_CALLBACK_LENGTH))],
            vol.Length(max=MAX_OAUTH_CALLBACKS),
        ),
        vol.Exclusive("reset", "change"): True,
    }


def _server_entry_options(hass: HomeAssistant) -> tuple[Any, dict[str, Any]]:
    from homeassistant.exceptions import HomeAssistantError

    entry = _find_server_config_entry(hass)
    if entry is None:
        raise HomeAssistantError("no ha_mcp_tools in-process server config entry")
    options = getattr(entry, "options", None)
    return entry, dict(options) if isinstance(options, Mapping) else {}


def _describe(entry: Any, options: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "entry_id": getattr(entry, "entry_id", None),
        "allowlist": effective_allowlist(options),
        "default_allowlist": list(DEFAULT_OAUTH_REDIRECT_ALLOWLIST),
        "customized": isinstance(options.get(OPT_OAUTH_REDIRECT_ALLOWLIST), list),
        # The list only gates none mode; the panel says so when it is not live.
        "applies": bool(options.get(OPT_ENABLE_WEBHOOK, True))
        and options.get(OPT_WEBHOOK_AUTH, WEBHOOK_AUTH_NONE) == WEBHOOK_AUTH_NONE,
    }


def _do_oauth_callbacks(hass: HomeAssistant, params: dict[str, Any]) -> dict[str, Any]:
    """The allowlist in force on the server entry, and whether it is live."""
    entry, options = _server_entry_options(hass)
    return _describe(entry, options)


def _do_oauth_callbacks_update(
    hass: HomeAssistant, params: dict[str, Any], *, result: dict[str, Any]
) -> dict[str, Any]:
    """Pure formatter; :func:`_oauth_callbacks_update_prep` does the write."""
    return result


async def _oauth_callbacks_update_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    """Validate and save the list; an invalid entry saves nothing.

    Returns ``{"result": {..., "saved": bool, "invalid": [...]}}`` so the
    panel can point at the entries it must fix.
    """
    entry, options = _server_entry_options(hass)
    if msg.get("reset"):
        options.pop(OPT_OAUTH_REDIRECT_ALLOWLIST, None)
    elif "allowlist" in msg:
        entries, invalid = normalize_allowlist(msg["allowlist"])
        if invalid:
            return {
                "result": {
                    **_describe(entry, options),
                    "saved": False,
                    "invalid": invalid,
                }
            }
        if (stored := stored_allowlist(options, entries)) is not None:
            options[OPT_OAUTH_REDIRECT_ALLOWLIST] = stored
    else:
        from homeassistant.exceptions import HomeAssistantError

        raise HomeAssistantError("oauth_callbacks_update needs allowlist or reset")
    hass.config_entries.async_update_entry(entry, options=options)
    return {"result": {**_describe(entry, options), "saved": True, "invalid": []}}


def command_specs() -> list[tuple[dict[Any, Any], Any, Any]]:
    """Rows for :func:`websocket_api._command_specs`."""
    return [
        (_oauth_callbacks_schema(), _do_oauth_callbacks, None),
        (
            _oauth_callbacks_update_schema(),
            _do_oauth_callbacks_update,
            _oauth_callbacks_update_prep,
        ),
    ]
