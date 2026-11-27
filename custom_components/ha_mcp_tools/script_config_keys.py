"""The top-level keys of a script config, for ``ha_mcp_tools/script_config_keys``.

Read from Core's script schema and the blueprint instance fields a script
config takes instead, so the server's hint for one passed as a tool argument
(move it inside ``config``) follows the running Core rather than the hand-kept
set it falls back to without this command.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

WS_SCRIPT_CONFIG_KEYS = "ha_mcp_tools/script_config_keys"
# Advertised by websocket_api's info; without it, or when the command is
# refused or unanswered, the server falls back to its hand-kept set.
CAPABILITY = "script_config_keys"


def script_config_keys(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    """The keys Core's ``SCRIPT_ENTITY_SCHEMA`` and ``BLUEPRINT_INSTANCE_FIELDS``
    take. A Core without either fails the command, which the server answers
    like a missing component."""
    from homeassistant.components.blueprint import schemas as blueprint
    from homeassistant.components.script import config as script

    keys = {
        *script.SCRIPT_ENTITY_SCHEMA.schema,
        *blueprint.BLUEPRINT_INSTANCE_FIELDS.schema,
    }
    return {"keys": sorted(str(key) for key in keys)}


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    """The (schema, handler, async prep) row websocket_api registers; no prep."""
    return [({vol.Required("type"): WS_SCRIPT_CONFIG_KEYS}, script_config_keys, None)]
