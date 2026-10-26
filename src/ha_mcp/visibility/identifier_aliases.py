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


def _normalize(value: str) -> str:
    return value.strip().casefold()


def build_identifier_aliases(
    hidden: Collection[str], registry_result: Any
) -> dict[str, frozenset[str]]:
    """Map each aliased domain to the object IDs and unique_ids of its hidden entities."""
    aliases: dict[str, set[str]] = {}

    def add(entity_id: str, alias: str) -> None:
        domain = entity_id.partition(".")[0]
        if domain in _ALIASED_DOMAINS and alias:
            aliases.setdefault(domain, set()).add(_normalize(alias))

    for hidden_id in hidden:
        add(hidden_id, hidden_id.partition(".")[2])
    entries = (
        registry_result.get("result") if isinstance(registry_result, dict) else None
    )
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        entity_id = entry.get("entity_id")
        unique_id = entry.get("unique_id")
        if (
            isinstance(entity_id, str)
            and entity_id in hidden
            and isinstance(unique_id, str)
        ):
            add(entity_id, unique_id)
    return {domain: frozenset(values) for domain, values in aliases.items()}


def hidden_identifier_argument(
    tool_name: str,
    args: Mapping[str, Any],
    aliases: Mapping[str, frozenset[str]],
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
    key = _normalize(value).removeprefix(f"{domain}.")
    return value if key in aliases.get(domain, frozenset()) else None


def _hidden_script_service(
    args: Mapping[str, Any], aliases: Mapping[str, frozenset[str]]
) -> str | None:
    # Home Assistant registers every script as a ``script.<storage key>`` service.
    domain = args.get("domain")
    service = args.get("service")
    if not (isinstance(domain, str) and isinstance(service, str)):
        return None
    if _normalize(domain) != "script":
        return None
    return (
        service if _normalize(service) in aliases.get("script", frozenset()) else None
    )
