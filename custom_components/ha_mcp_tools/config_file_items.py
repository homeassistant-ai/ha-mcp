"""The scripts, scenes and automations a config file defines, for
``ha_mcp_tools/config_file_items``.

ha-mcp's enforce mode refuses to read or edit a file that defines a hidden
item. ``!include``, ``!include_dir_*`` and packages can put a definition, or
part of one, in any file, and only Core's loader knows which: it names the file
it read each string, mapping and list from (``__config_file__``). So the
component loads the configuration with Core's loader and names the items whose
definition draws on a requested file. A file Core does not load defines
nothing.

The configuration is loaded the way ``yaml_find`` loads it, without Core's
package merge: merging sets up each package's integrations, logs their errors
on every load, and drops packages Core rejects.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from annotatedyaml import SECRET_YAML, Secrets
from homeassistant.config import (
    YAML_CONFIG_FILE,
    extract_domain_configs,
    load_yaml_config_file,
)
from homeassistant.const import CONF_ID, CONF_PACKAGES
from homeassistant.core import DOMAIN as HOMEASSISTANT_DOMAIN
from homeassistant.core import HomeAssistant

from .const import TAILED_LOG_FILES

WS_CONFIG_FILE_ITEMS = "ha_mcp_tools/config_file_items"
# Advertised by websocket_api's info; without it enforce mode cannot tell
# which file defines a hidden item and refuses config-file reads and edits.
CAPABILITY = "config_file_items"
# Scripts are keyed by their key, scenes and automations by their config id.
_DOMAINS = ("automation", "scene", "script")
# Core writes its logs and never loads them; a secret's value carries
# secrets.yaml's name into every item using it, and that read is masked.
_NEVER_DEFINES = TAILED_LOG_FILES | {SECRET_YAML}


def config_file_items(
    hass: HomeAssistant, msg: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    """The items found by ``_load``, or why the configuration did not load."""
    return result


async def _load(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    paths = [
        p
        for p in msg["paths"]
        if os.path.normpath(p) not in _NEVER_DEFINES  # noqa: ASYNC240
    ]
    if not paths:
        return {"result": {domain: [] for domain in _DOMAINS}}
    try:
        items = await hass.async_add_executor_job(
            _items_in, hass.config.config_dir, paths
        )
    except Exception as err:  # noqa: BLE001
        # A YAML error, a missing include or an include loop alike leave the
        # files unchecked; the caller refuses them.
        return {"result": {"error": str(err) or type(err).__name__}}
    return {"result": items}


def _items_in(config_dir: str, paths: list[str]) -> dict[str, list[str]]:
    """Per domain, the items whose definition reads any of ``paths``."""
    conf = load_yaml_config_file(
        os.path.join(config_dir, YAML_CONFIG_FILE), Secrets(Path(config_dir))
    )
    real = functools.cache(os.path.realpath)
    wanted = {real(os.path.join(config_dir, path)) for path in paths}
    found: dict[str, set[str]] = {domain: set() for domain in _DOMAINS}
    for domain, key, body in _definitions(conf):
        if any(real(name) in wanted for name in _files(body)):
            found[domain].add(key)
    return {domain: sorted(keys) for domain, keys in found.items()}


def _definitions(conf: dict[str, Any]) -> Iterator[tuple[str, str, Any]]:
    """Every (domain, key, body) in the configuration and its packages, each
    package walked as written."""
    core = conf.get(HOMEASSISTANT_DOMAIN)
    packages = core.get(CONF_PACKAGES) if isinstance(core, dict) else None
    sources = [conf, *(packages.values() if isinstance(packages, dict) else ())]
    for source in sources:
        if not isinstance(source, dict):
            continue
        for domain in _DOMAINS:
            for section_key in extract_domain_configs(source, domain):
                section = source[section_key]
                if domain == "script":
                    if isinstance(section, dict):
                        for key, body in section.items():
                            yield domain, str(key), body
                    continue
                for body in section if isinstance(section, list) else [section]:
                    if isinstance(body, dict) and CONF_ID in body:
                        yield domain, str(body[CONF_ID]), body


def _files(body: Any) -> Iterator[str]:
    """The files named on a definition and everything nested in it.

    ``!include`` names the included value after the including file and
    ``!include_dir_merge_list`` keeps only each file's items, so such a file
    shows only through the strings, string keys, mappings and lists inside it.
    """
    stack = [body]
    while stack:
        node = stack.pop()
        if (name := getattr(node, "__config_file__", None)) is not None:
            yield name
        if isinstance(node, dict):
            stack.extend(node)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    """The (schema, handler, async prep) row websocket_api registers."""
    return [
        (
            {
                vol.Required("type"): WS_CONFIG_FILE_ITEMS,
                vol.Required("paths"): [str],
            },
            config_file_items,
            _load,
        )
    ]
