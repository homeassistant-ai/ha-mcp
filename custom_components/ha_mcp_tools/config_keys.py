"""The top-level keys of an automation or script config, for ``ha_mcp_tools/config_keys``.

Read from Core's config schema for the domain and the blueprint instance
fields a config takes instead, so the server's hint for one passed as a tool
argument (move it inside ``config``) follows the running Core. Without this
command the server has no list and says it could not read Core's fields.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

WS_CONFIG_KEYS = "ha_mcp_tools/config_keys"
# Advertised by websocket_api's info; without it, or when the command is
# refused or unanswered, the server says Core's fields could not be read.
CAPABILITY = "config_keys"
DOMAINS = ("automation", "script")


def config_keys(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    """The keys Core's schema for ``msg["domain"]`` takes (``PLATFORM_SCHEMA``
    for an automation, ``SCRIPT_ENTITY_SCHEMA`` for a script) plus
    ``BLUEPRINT_INSTANCE_FIELDS``. A Core without them fails the command; the
    server then has no key list, as without the component."""
    from homeassistant.components.automation import config as automation
    from homeassistant.components.blueprint import schemas as blueprint
    from homeassistant.components.script import config as script

    if msg["domain"] == "script":
        schema, renames = script.SCRIPT_ENTITY_SCHEMA, []
    else:
        # PLATFORM_SCHEMA wraps the config schema in renames and deprecations.
        validators = automation.PLATFORM_SCHEMA.validators
        schema = next(
            v for v in validators if isinstance(getattr(v, "schema", None), dict)
        )
        renames = [v for v in validators if v is not schema]
    keys = {str(key) for key in schema.schema}
    # Core still takes the singular trigger/condition/action and renames them;
    # keep each singular its validators turn into a schema key.
    for key in [k for k in keys if k.endswith("s")]:
        probe: Any = {key[:-1]: None}
        for rename in renames:
            probe = rename(probe)
        if key in probe:
            keys.add(key[:-1])
    keys.update(str(key) for key in blueprint.BLUEPRINT_INSTANCE_FIELDS.schema)
    return {"keys": sorted(keys)}


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    """The (schema, handler, async prep) row websocket_api registers; no prep."""
    return [
        (
            {
                vol.Required("type"): WS_CONFIG_KEYS,
                vol.Required("domain"): vol.In(DOMAINS),
            },
            config_keys,
            None,
        )
    ]
