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
from collections.abc import Iterable, Mapping
from functools import partial
from typing import TYPE_CHECKING, Any

from annotatedyaml import SECRET_YAML
from homeassistant.config import AUTOMATION_CONFIG_PATH, SCRIPT_CONFIG_PATH
from homeassistant.const import (
    ATTR_DOMAIN,
    ATTR_SERVICE,
    CONF_ID,
    EVENT_CALL_SERVICE,
    SERVICE_RELOAD,
)
from homeassistant.core import callback
from homeassistant.helpers.start import async_at_started

from .config_file_items import _files
from .websocket_api.secrets import (
    REDACTED,
    _load_secret_scrub,
    _scrub_secret_values,
)

if TYPE_CHECKING:
    from homeassistant.core import Event, HomeAssistant

WS_LOADED_CONFIG = "ha_mcp_tools/loaded_config"
# hass.data: every secret value read so far. A number carries no trace of the
# secrets.yaml it came from, so one Core resolved before that file changed or
# went away is redacted only if the file was read here while it held the number.
_SEEN = "ha_mcp_tools_secret_values_seen"
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
    loaded: tuple[str, Any, Any],
) -> dict[str, Any]:
    domain, entity, raw = loaded
    if not isinstance(raw, dict):
        return dict(_NOT_LOADED)
    file, line = _declared_at(hass, domain, entity, raw)
    return {
        "loaded": True,
        "config": _redacted(raw, secret_values),
        "file": file,
        "line": line if isinstance(line, int) else None,
        # Core's config API reads it: an automation needs its id for that. Seen
        # for an item removed there while Core still holds it, until the reload.
        "in_storage": file == _STORAGE[domain]
        and (domain == "script" or CONF_ID in raw),
        # A secrets.yaml could not be read: a secret in config may not be redacted.
        "scrub_degraded": scrub_degraded,
    }


def _redacted(node: Any, secret_values: frozenset[str]) -> Any:
    """``node`` without the secrets Core resolved into it.

    Core's loader names the file each string, mapping and list came from, so a
    value read from a ``secrets.yaml`` is redacted whatever that file holds
    now. A number carries no such name and is matched against every secret
    value read so far.
    """
    source = getattr(node, "__config_file__", None)
    if source is not None and os.path.basename(source) == SECRET_YAML:
        return REDACTED
    if isinstance(node, dict):
        result: dict[Any, Any] = {}
        for key, value in node.items():
            # A key can be a !secret too. Its placeholder must not replace
            # another entry: an earlier secret key's, or a key the mapping keeps.
            shown = _redacted(key, secret_values)
            taken = 1
            while shown != key and (shown in result or shown in node):
                taken += 1
                shown = f"{REDACTED} ({taken})"
            result[shown] = _redacted(value, secret_values)
        return result
    if isinstance(node, list):
        return [_redacted(item, secret_values) for item in node]
    return _scrub_secret_values(node, secret_values)


def _loaded(hass: HomeAssistant, entity_id: str) -> tuple[str, Any, Any]:
    """The domain, the loaded entity and its ``raw_config`` (None when absent)."""
    domain = entity_id.partition(".")[0]
    component = hass.data.get(domain)
    entity = component.get_entity(entity_id) if component is not None else None
    return domain, entity, getattr(entity, "raw_config", None)


async def _secret_values(
    hass: HomeAssistant, files: Iterable[str]
) -> tuple[frozenset[str], bool]:
    """Every secret value read so far, and whether a secrets file was unreadable.

    ``files`` are YAML files Core loaded: Core resolves a !secret from the
    secrets.yaml nearest the file that names it, and those are read now.
    """
    values, degraded = await hass.async_add_executor_job(
        _load_secret_scrub, hass, sorted(set(files))
    )
    seen: set[str] = hass.data.setdefault(_SEEN, set())
    seen.update(values)
    return frozenset(seen), degraded


async def _prep(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    # Read once: a reload during the file read below must not swap in another
    # definition than the one the secrets were read for.
    loaded = _loaded(hass, msg["entity_id"])
    values, degraded = await _secret_values(hass, _files(loaded[2]))
    return {"secret_values": values, "scrub_degraded": degraded, "loaded": loaded}


async def _read_loaded_secrets(
    hass: HomeAssistant, _event: Event | None = None
) -> None:
    """Read the secrets files every loaded automation and script draws on."""
    files: set[str] = set()
    for domain in _STORAGE:
        component = hass.data.get(domain)
        for entity in component.entities if component is not None else ():
            files.update(_files(getattr(entity, "raw_config", None)))
    await _secret_values(hass, files)


@callback
def _is_script_reload(data: Mapping[str, Any]) -> bool:
    return (
        data.get(ATTR_DOMAIN) == "script" and data.get(ATTR_SERVICE) == SERVICE_RELOAD
    )


@callback
def async_follow_loads(hass: HomeAssistant) -> None:
    """Read the secrets when Core loads them, so a number that later changes in
    its file stays redacted. Like the commands, this lasts until a restart.
    """
    if _SEEN in hass.data:
        return
    hass.data[_SEEN] = set()
    async_at_started(hass, _read_loaded_secrets)
    # Fired once an automation reload is done.
    hass.bus.async_listen("automation_reloaded", partial(_read_loaded_secrets, hass))
    # A script reload fires nothing of its own; Core announces the call first.
    hass.bus.async_listen(
        EVENT_CALL_SERVICE,
        partial(_read_loaded_secrets, hass),
        event_filter=_is_script_reload,
    )


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
