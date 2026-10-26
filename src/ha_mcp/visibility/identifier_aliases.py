"""Bare-identifier aliases for hidden scripts, scenes and automations.

The script, scene and automation config tools address their target by a bare
storage key (``unlock_house``) or config ``id`` as well as by entity_id, and
derive the entity themselves after the inbound scan has run. That key is the
entity's registry ``unique_id`` (Home Assistant Core sets a script's to its
storage key and a scene's or automation's to its config ``id``), which differs
from the object ID after a rename and is numeric for UI-created scenes and
automations. Only the entity registry maps it back to the hidden entity_id, so
the aliases are derived from the registry the hidden-set refresh already fetched.

Home Assistant also registers every script as a ``script.<storage key>``
service, so ``ha_call_service`` with ``domain: script`` is checked the same way.

The check is scoped to these identifier arguments, so the same word appearing
in template text or a message never triggers it.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

BARE_IDENTIFIER_ARGS: dict[str, tuple[str, str]] = {
    "ha_config_get_script": ("script_id", "script"),
    "ha_config_set_script": ("script_id", "script"),
    "ha_config_remove_script": ("script_id", "script"),
    "ha_config_get_scene": ("scene_id", "scene"),
    "ha_config_set_scene": ("scene_id", "scene"),
    "ha_config_remove_scene": ("scene_id", "scene"),
    "ha_config_get_automation": ("identifier", "automation"),
    "ha_config_set_automation": ("identifier", "automation"),
    "ha_config_remove_automation": ("identifier", "automation"),
}

_SERVICE_TOOL = "ha_call_service"

_ALIASED_DOMAINS = frozenset(domain for _arg, domain in BARE_IDENTIFIER_ARGS.values())


@dataclass(frozen=True)
class IdentifierAliases:
    """Identifiers, per domain, that address a hidden script, scene or automation.

    ``object_ids`` are casefolded: a tool that cannot resolve a key falls back to
    ``<domain>.<key>``, which Home Assistant lowercases. ``storage_keys`` (the
    registry ``unique_id``) match exactly, like the tools' registry lookup, so a
    visible ``door`` is not concealed by a hidden ``Door``. ``script_services``
    are the casefolded service names (storage keys) of hidden scripts.
    """

    object_ids: Mapping[str, frozenset[str]] = field(default_factory=dict)
    storage_keys: Mapping[str, frozenset[str]] = field(default_factory=dict)
    script_services: frozenset[str] = frozenset()

    def addresses_hidden(self, domain: str, value: str) -> bool:
        key = value.strip().removeprefix(f"{domain}.")
        if key in self.storage_keys.get(domain, frozenset()):
            return True
        folded = key.casefold().removeprefix(f"{domain}.")
        return folded in self.object_ids.get(domain, frozenset())


def build_identifier_aliases(
    hidden: Collection[str], registry_result: Any
) -> IdentifierAliases:
    """Collect the object IDs and registry unique_ids of hidden aliased entities."""
    object_ids: dict[str, set[str]] = {}
    storage_keys: dict[str, set[str]] = {}
    hidden_scripts: dict[str, str] = {}
    for hidden_id in hidden:
        domain, _, object_id = hidden_id.partition(".")
        if domain in _ALIASED_DOMAINS and object_id:
            object_ids.setdefault(domain, set()).add(object_id.casefold())
            if domain == "script":
                hidden_scripts[hidden_id] = object_id
    entries = (
        registry_result.get("result") if isinstance(registry_result, dict) else None
    )
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        entity_id = entry.get("entity_id")
        unique_id = entry.get("unique_id")
        if not (
            isinstance(entity_id, str)
            and entity_id in hidden
            and isinstance(unique_id, str)
            and unique_id
        ):
            continue
        domain = entity_id.partition(".")[0]
        if domain in _ALIASED_DOMAINS:
            storage_keys.setdefault(domain, set()).add(unique_id)
            if domain == "script":
                hidden_scripts[entity_id] = unique_id
    return IdentifierAliases(
        object_ids={d: frozenset(v) for d, v in object_ids.items()},
        storage_keys={d: frozenset(v) for d, v in storage_keys.items()},
        script_services=frozenset(key.casefold() for key in hidden_scripts.values()),
    )


def hidden_identifier_argument(
    tool_name: str, args: Mapping[str, Any], aliases: IdentifierAliases
) -> str | None:
    """Return the caller's identifier argument when it addresses a hidden entity."""
    if tool_name == _SERVICE_TOOL:
        return _hidden_script_service(args, aliases)
    spec = BARE_IDENTIFIER_ARGS.get(tool_name)
    if spec is None:
        return None
    arg_name, domain = spec
    value = args.get(arg_name)
    if not isinstance(value, str):
        return None
    return value if aliases.addresses_hidden(domain, value) else None


def _hidden_script_service(
    args: Mapping[str, Any], aliases: IdentifierAliases
) -> str | None:
    # Home Assistant registers every script as a ``script.<storage key>`` service.
    domain = args.get("domain")
    service = args.get("service")
    if not (isinstance(domain, str) and isinstance(service, str)):
        return None
    if domain.strip().casefold() != "script":
        return None
    hit = service.strip().casefold() in aliases.script_services
    return service if hit else None
