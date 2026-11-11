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
service, so ``ha_call_service`` with ``domain: script`` is checked the same way,
and ``script.<storage key>`` joins the terms the embedded scans match. Traces
are keyed by storage key too, so trace records are checked against it (config
files are checked in ``config_files``).

The argument check is scoped to these identifier arguments, so the same word
appearing in template text or a message never triggers it.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

BARE_IDENTIFIER_ARGS: dict[str, tuple[str, str]] = {
    "ha_config_get_script": ("script_id", "script"),
    "ha_config_set_script": ("script_id", "script"),
    "ha_config_remove_script": ("script_id", "script"),
    "ha_config_get_scene": ("scene_id", "scene"),
    "ha_config_set_scene": ("scene_id", "scene"),
    "ha_config_remove_scene": ("scene_id", "scene"),
    "ha_config_get_automation": ("entity_id", "automation"),
    "ha_config_set_automation": ("entity_id", "automation"),
    "ha_config_remove_automation": ("entity_id", "automation"),
}

_SERVICE_TOOL = "ha_call_service"
_TRACE_TOOL = "ha_get_automation_traces"

_ALIASED_DOMAINS = frozenset(domain for _arg, domain in BARE_IDENTIFIER_ARGS.values())
# The automation tools read a bare ``entity_id`` only as a config ``id``, so an
# automation's object ID would only ever match a visible automation.
_OBJECT_ID_DOMAINS = frozenset({"script", "scene"})
# Trace records and raw trace commands name their target by ``domain`` + ``item_id``.
_TRACE_DOMAINS = frozenset({"script", "automation"})


@dataclass(frozen=True)
class IdentifierAliases:
    """Identifiers, per domain, that address a hidden script, scene or automation.

    ``object_ids`` are casefolded: a tool that cannot resolve a key falls back to
    ``<domain>.<key>``, which Home Assistant lowercases. ``storage_keys`` (the
    registry ``unique_id``) match exactly, like the tools' registry lookup, so a
    visible ``door`` is not concealed by a hidden ``Door``. ``script_services``
    are the casefolded service names (storage keys) of hidden scripts.
    ``entity_unique_ids`` maps every trace-target entity_id the registry read saw
    to its ``unique_id``, hidden or not, so the trace tool's entity_id can be
    resolved the way that tool resolves it.
    """

    object_ids: Mapping[str, frozenset[str]] = field(default_factory=dict)
    storage_keys: Mapping[str, frozenset[str]] = field(default_factory=dict)
    script_services: frozenset[str] = frozenset()
    entity_unique_ids: Mapping[str, str] = field(default_factory=dict)

    def embedded_terms(self) -> frozenset[str]:
        """Strings that run a hidden script without naming its entity_id."""
        return frozenset(f"script.{key}" for key in self.script_services)

    def addresses_hidden(self, domain: str, value: str) -> bool:
        key = value.strip().removeprefix(f"{domain}.")
        if key in self.storage_keys.get(domain, frozenset()):
            return True
        folded = key.casefold().removeprefix(f"{domain}.")
        return folded in self.object_ids.get(domain, frozenset())

    def addresses_hidden_trace_item(self, domain: str, value: str) -> bool:
        """Whether a trace's ``item_id`` is the registry key of a hidden entity.

        Traces are keyed by the registry ``unique_id``; an object ID is not a
        trace key, so a visible script whose storage key matches a hidden
        script's object ID must not be concealed.
        """
        return value.strip() in self.storage_keys.get(domain, frozenset())


def _registry_key(entry: Any) -> tuple[str, str] | None:
    """The entity_id and registry ``unique_id`` of a usable registry row."""
    if not isinstance(entry, dict):
        return None
    entity_id = entry.get("entity_id")
    unique_id = entry.get("unique_id")
    if isinstance(entity_id, str) and isinstance(unique_id, str) and unique_id:
        return entity_id, unique_id
    return None


def _collect_registry_aliases(
    hidden: Collection[str], registry_result: Any, hidden_scripts: dict[str, str]
) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Read hidden keys and all trace-target entity keys from the registry rows."""
    storage_keys: dict[str, set[str]] = {}
    entity_unique_ids: dict[str, str] = {}
    entries = (
        registry_result.get("result") if isinstance(registry_result, dict) else None
    )
    for entry in entries if isinstance(entries, list) else []:
        parsed = _registry_key(entry)
        if parsed is None:
            continue
        entity_id, unique_id = parsed
        domain = entity_id.partition(".")[0]
        if domain in _TRACE_DOMAINS:
            entity_unique_ids[entity_id] = unique_id
        if entity_id not in hidden or domain not in _ALIASED_DOMAINS:
            continue
        storage_keys.setdefault(domain, set()).add(unique_id)
        if domain == "script":
            hidden_scripts[entity_id] = unique_id
    return storage_keys, entity_unique_ids


def build_identifier_aliases(
    hidden: Collection[str], registry_result: Any
) -> IdentifierAliases:
    """Collect the keys, and the trace entity keys, of hidden aliased entities."""
    object_ids: dict[str, set[str]] = {}
    hidden_scripts: dict[str, str] = {}
    for hidden_id in hidden:
        domain, _, object_id = hidden_id.partition(".")
        if domain in _ALIASED_DOMAINS and object_id:
            if domain in _OBJECT_ID_DOMAINS:
                object_ids.setdefault(domain, set()).add(object_id.casefold())
            if domain == "script":
                hidden_scripts[hidden_id] = object_id
    storage_keys, entity_unique_ids = _collect_registry_aliases(
        hidden, registry_result, hidden_scripts
    )
    return IdentifierAliases(
        object_ids={d: frozenset(v) for d, v in object_ids.items()},
        storage_keys={d: frozenset(v) for d, v in storage_keys.items()},
        script_services=frozenset(key.casefold() for key in hidden_scripts.values()),
        entity_unique_ids=entity_unique_ids,
    )


def _is_trace_command(command: Any) -> bool:
    return isinstance(command, str) and command.strip().casefold().startswith("trace/")


def is_trace_query(tool_name: str, args: Mapping[str, Any]) -> bool:
    """Whether a call reads stored traces, keyed by hidden storage keys."""
    if tool_name == _TRACE_TOOL:
        return True
    return tool_name == _SERVICE_TOOL and _is_trace_command(args.get("ws_command"))


def hidden_identifier_argument(
    tool_name: str, args: Mapping[str, Any], aliases: IdentifierAliases
) -> str | None:
    """Return the caller's identifier argument when it addresses a hidden entity."""
    if tool_name == _SERVICE_TOOL:
        return _hidden_script_service(args, aliases) or _hidden_trace_item(
            _json_mapping(args.get("data")), aliases
        )
    if tool_name == _TRACE_TOOL:
        trace_hit = _hidden_trace_item(args, aliases)
        if trace_hit is not None:
            return trace_hit
        entity_id = args.get("entity_id")
        if not isinstance(entity_id, str):
            return None
        return _hidden_trace_entity_id(entity_id, aliases)
    spec = BARE_IDENTIFIER_ARGS.get(tool_name)
    if spec is None:
        return None
    arg_name, domain = spec
    value = args.get(arg_name)
    if not isinstance(value, str):
        return None
    return value if aliases.addresses_hidden(domain, value) else None


def _hidden_trace_entity_id(entity_id: str, aliases: IdentifierAliases) -> str | None:
    """Whether the trace tool's ``entity_id`` resolves to a hidden trace key.

    The tool resolves an entity_id to its registry ``unique_id`` and falls back
    to the object ID only when the registry has no answer, so a visible
    entity's object ID that matches a hidden storage key is not concealed.
    Without a registry row the tool would use the object ID, so the alias check
    still fails closed.
    """
    value = entity_id.strip()
    domain = value.partition(".")[0]
    unique_id = aliases.entity_unique_ids.get(value)
    if unique_id is not None:
        hit = unique_id in aliases.storage_keys.get(domain, frozenset())
        return value if hit else None
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


def _json_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, Mapping) else None


def _hidden_trace_item(
    record: Mapping[str, Any] | None, aliases: IdentifierAliases
) -> str | None:
    """Return the ``item_id`` of a ``domain`` + ``item_id`` record naming a hidden one."""
    if record is None:
        return None
    domain = record.get("domain")
    item_id = record.get("item_id")
    if not (isinstance(domain, str) and isinstance(item_id, str | int)):
        return None
    domain = domain.strip().casefold()
    if domain in _TRACE_DOMAINS and aliases.addresses_hidden_trace_item(
        domain, str(item_id)
    ):
        return str(item_id)
    return None


def _names_hidden_trace_item(value: Any, aliases: IdentifierAliases) -> bool:
    if isinstance(value, Mapping):
        if _hidden_trace_item(value, aliases) is not None:
            return True
        return any(_names_hidden_trace_item(item, aliases) for item in value.values())
    if isinstance(value, list | tuple):
        return any(_names_hidden_trace_item(item, aliases) for item in value)
    return False


def result_reveals_hidden(payloads: Iterable[Any], aliases: IdentifierAliases) -> bool:
    """Whether a result's trace records name a hidden script or automation.

    ``payloads`` are the result's JSON values (structured content and parsed
    text blocks). Trace records (``trace/list``, ``trace/contexts``) name their
    target by ``domain`` + ``item_id`` rather than by entity_id.
    """
    return any(_names_hidden_trace_item(payload, aliases) for payload in payloads)
