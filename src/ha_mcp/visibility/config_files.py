"""Hidden script, scene and automation definitions in Home Assistant config files.

``scripts.yaml`` is keyed by storage key and ``scenes.yaml`` / ``automations.yaml``
list items by config ``id`` (Core's ``SCRIPT_CONFIG_PATH`` / ``SCENE_CONFIG_PATH``
/ ``AUTOMATION_CONFIG_PATH``), and a package or ``configuration.yaml`` defines
them under a ``script:`` / ``scene:`` / ``automation:`` section. A hidden body in
such a file never spells out its entity_id, so the entity scan cannot see it.

Only those definition positions count: a helper or dashboard key that merely
shares a hidden script's key is not a definition and stays readable.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

import yaml  # type: ignore[import-untyped]

from .identifier_aliases import IdentifierAliases

_ROOT_FILE_DOMAINS = {
    "scripts.yaml": "script",
    "scenes.yaml": "scene",
    "automations.yaml": "automation",
}
# Home Assistant also reads ``<domain> <label>:`` sections (``automation old:``).
_SECTION_KEY = re.compile(r"^(script|scene|automation)(?: .+)?$")
_YAML_SUFFIXES = (".yaml", ".yml")


class _TagTolerantLoader(yaml.SafeLoader):
    """SafeLoader that neutralizes HA tags (``!include``, ``!secret``) to None."""


_TagTolerantLoader.add_multi_constructor("!", lambda _loader, _suffix, _node: None)


def is_yaml_path(path: str) -> bool:
    return path.casefold().endswith(_YAML_SUFFIXES)


def defines_hidden_item(path: str, content: str, aliases: IdentifierAliases) -> bool:
    """Whether a config file's full ``content`` defines a hidden item.

    An unparseable file is judged by whether its text contains a hidden key at
    all, failing closed rather than letting a broken file through unchecked.
    """
    try:
        document = yaml.load(content, Loader=_TagTolerantLoader)
    except yaml.YAMLError:
        return _mentions_hidden_key(content, aliases)
    # The component normalizes the path the same way before reading it, so
    # ``.//scripts.yaml`` and ``www/../scripts.yaml`` are ``scripts.yaml``.
    root_domain = _ROOT_FILE_DOMAINS.get(posixpath.normpath(path.strip()))
    if root_domain is not None and _section_defines_hidden(
        root_domain, document, aliases
    ):
        return True
    return _any_section_defines_hidden(document, aliases)


def _any_section_defines_hidden(value: Any, aliases: IdentifierAliases) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            match = _SECTION_KEY.match(key) if isinstance(key, str) else None
            if match and _section_defines_hidden(match.group(1), item, aliases):
                return True
            if _any_section_defines_hidden(item, aliases):
                return True
    elif isinstance(value, list):
        return any(_any_section_defines_hidden(item, aliases) for item in value)
    return False


def _section_defines_hidden(
    domain: str, section: Any, aliases: IdentifierAliases
) -> bool:
    if domain == "script":
        return isinstance(section, dict) and any(
            isinstance(key, str) and key.casefold() in aliases.script_services
            for key in section
        )
    items = section if isinstance(section, list) else [section]
    config_ids = aliases.storage_keys.get(domain, frozenset())
    return any(
        isinstance(item, dict)
        and isinstance(item.get("id"), str | int)
        and str(item["id"]) in config_ids
        for item in items
    )


def _mentions_hidden_key(content: str, aliases: IdentifierAliases) -> bool:
    keys = set(aliases.script_services)
    keys |= aliases.storage_keys.get("scene", frozenset())
    keys |= aliases.storage_keys.get("automation", frozenset())
    folded = content.casefold()
    return any(
        re.search(rf"(?<![\w-]){re.escape(key.casefold())}(?![\w-])", folded)
        for key in keys
    )
