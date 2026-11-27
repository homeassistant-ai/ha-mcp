"""The automation or script definition Core has loaded, and the file it came
from, for ``ha_mcp_tools/loaded_config``.

Core's config API stores automations in ``automations.yaml`` and scripts in
``scripts.yaml`` and reads nothing else. One defined in a package or another
YAML file exists only as Core loaded it: the entity's ``raw_config``, which
Core's ``automation/config`` and ``script/config`` WebSocket commands send.
This command sends the same body, with ``secrets.yaml`` values redacted, and
adds what those commands cannot: the file and line Core's YAML loader read the
definition from (``__config_file__`` / ``__line__``).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from homeassistant.config import AUTOMATION_CONFIG_PATH, SCRIPT_CONFIG_PATH
from homeassistant.const import CONF_ID

from .config_file_items import _files
from .websocket_api.secrets import _load_secret_scrub, _scrub_secret_values

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

WS_LOADED_CONFIG = "ha_mcp_tools/loaded_config"
# Advertised by websocket_api's info; without it the server reads the body
# from Core's own WebSocket command and cannot name the file.
CAPABILITY = "loaded_config"
# The file Core's config API stores each domain in.
_STORAGE = {"automation": AUTOMATION_CONFIG_PATH, "script": SCRIPT_CONFIG_PATH}
_NOT_LOADED: dict[str, Any] = {
    "loaded": False,
    "config": None,
    "file": None,
    "line": None,
    "in_storage": False,
    "scrub_degraded": False,
}


def _declared_at(
    hass: HomeAssistant, domain: str, entity: Any, raw: dict[Any, Any]
) -> tuple[str | None, Any]:
    """The file, relative to the config directory, and line a definition's own
    keys were read from.

    A blueprint item's ``raw_config`` is the blueprint with its inputs filled
    in, so the keys the blueprint supplies name the blueprint's file; those
    are passed over.
    """
    blueprint = getattr(entity, "referenced_blueprint", None)
    blueprints = (hass.data.get("blueprint") or {}).get(domain)
    folder = getattr(blueprints, "blueprint_folder", None)
    skip = os.path.normpath(folder / blueprint) if blueprint and folder else None
    for key in raw:
        path = getattr(key, "__config_file__", None)
        if path is None or os.path.normpath(path) == skip:
            continue
        relative = os.path.relpath(path, hass.config.config_dir)
        inside = not relative.startswith(os.pardir)
        return (relative if inside else path), getattr(key, "__line__", None)
    return None, None


def loaded_config(
    hass: HomeAssistant,
    msg: dict[str, Any],
    *,
    secret_values: frozenset[str],
    scrub_degraded: bool,
) -> dict[str, Any]:
    domain, entity, raw = _loaded(hass, msg["entity_id"])
    if not isinstance(raw, dict):
        return dict(_NOT_LOADED)
    file, line = _declared_at(hass, domain, entity, raw)
    return {
        "loaded": True,
        "config": _scrub_secret_values(raw, secret_values),
        "file": file,
        "line": line if isinstance(line, int) else None,
        # Core's config API reads it: an automation needs its id for that. Seen
        # for an item removed there while Core still holds it, until the reload.
        "in_storage": file == _STORAGE[domain]
        and (domain == "script" or CONF_ID in raw),
        # secrets.yaml could not be read, so config is not redacted.
        "scrub_degraded": scrub_degraded,
    }


def _loaded(hass: HomeAssistant, entity_id: str) -> tuple[str, Any, Any]:
    """The domain, the loaded entity and its ``raw_config`` (None when absent)."""
    domain = entity_id.partition(".")[0]
    component = hass.data.get(domain)
    entity = component.get_entity(entity_id) if component is not None else None
    return domain, entity, getattr(entity, "raw_config", None)


async def _prep(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    # Every file the definition draws on: Core resolves a !secret from the
    # secrets.yaml nearest the file that names it.
    files = sorted(set(_files(_loaded(hass, msg["entity_id"])[2])))
    values, degraded = await hass.async_add_executor_job(
        _load_secret_scrub, hass, files
    )
    return {"secret_values": values, "scrub_degraded": degraded}


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    """The (schema, handler, async prep) row websocket_api registers."""
    return [
        (
            {
                vol.Required("type"): WS_LOADED_CONFIG,
                vol.Required("entity_id"): vol.Match(
                    rf"^({'|'.join(_STORAGE)})\.[^.]+$"
                ),
            },
            loaded_config,
            _prep,
        )
    ]
