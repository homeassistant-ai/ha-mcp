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

import functools
import os
import time
from typing import TYPE_CHECKING, Any

from annotatedyaml import SECRET_YAML
from homeassistant.config import AUTOMATION_CONFIG_PATH, SCRIPT_CONFIG_PATH
from homeassistant.const import CONF_ID

from .config_file_items import _files
from .websocket_api.secrets import (
    REDACTED,
    _load_secret_scrub,
    _scrub_secret_values,
    _secret_files,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

WS_LOADED_CONFIG = "ha_mcp_tools/loaded_config"
# The fallback for the process start.
_IMPORTED = time.time()
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
        # A secret in config may not be redacted: see _scrub_inputs.
        "scrub_degraded": scrub_degraded,
    }


def _redacted(node: Any, secret_values: frozenset[str]) -> Any:
    """``node`` without the secrets Core resolved into it.

    Core's loader names the file each string, mapping and list came from, so a
    value read from a ``secrets.yaml`` is redacted whatever that file holds
    now. A number carries no such name and is matched against the files'
    current values.
    """
    source = getattr(node, "__config_file__", None)
    if source is not None and os.path.basename(source) == SECRET_YAML:
        return REDACTED
    if isinstance(node, dict):
        result: dict[Any, Any] = {}
        for key, value in node.items():
            # A key can be a !secret too; a second one must not replace the first.
            shown = _redacted(key, secret_values)
            while shown == REDACTED and shown in result:
                shown = f"{REDACTED} ({len(result)})"
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


def _has_number(node: Any) -> bool:
    if isinstance(node, dict):
        return any(_has_number(value) for value in node.values())
    if isinstance(node, list):
        return any(_has_number(item) for item in node)
    return isinstance(node, (int, float)) and not isinstance(node, bool)


@functools.cache
def _process_started() -> float:
    """When this Home Assistant process started, before it loaded any config."""
    try:
        with open("/proc/self/stat", encoding="utf-8") as stat:
            ticks = int(stat.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/stat", encoding="utf-8") as stat:
            boot = next(int(line.split()[1]) for line in stat if line[:5] == "btime")
        return boot + ticks / os.sysconf("SC_CLK_TCK")
    except OSError, ValueError, IndexError, StopIteration:
        return _IMPORTED


def _changed_since_start(path: str) -> bool:
    """Whether a secrets.yaml may differ from the one Core read at its start.

    A missing one leaves no trace of its own: removing a file changes its
    directory, so the directory's change time stands in for it.
    """
    started = _process_started()
    try:
        return os.path.getmtime(path) > started
    except FileNotFoundError:
        try:
            return os.path.getmtime(os.path.dirname(path)) > started
        except OSError:
            return True
    except OSError:
        return True


def _scrub_inputs(hass: HomeAssistant, raw: Any) -> tuple[frozenset[str], bool]:
    """The values to scrub from ``raw`` and whether the scrub can miss one."""
    # Every file the definition draws on: Core resolves a !secret from the
    # secrets.yaml nearest the file that names it.
    files = sorted(set(_files(raw)))
    values, degraded = _load_secret_scrub(hass, files)
    # A number carries no source name: one Core resolved from a secrets.yaml
    # that has changed since cannot be told from the definition's own numbers.
    consulted = _secret_files(hass.config.path(SECRET_YAML), files)
    changed = any(_changed_since_start(path) for path in consulted)
    return values, degraded or (changed and _has_number(raw))


async def _prep(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    # Read once: a reload during the file read below must not swap in another
    # definition than the one the secrets were read for.
    loaded = _loaded(hass, msg["entity_id"])
    values, degraded = await hass.async_add_executor_job(_scrub_inputs, hass, loaded[2])
    return {"secret_values": values, "scrub_degraded": degraded, "loaded": loaded}


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
