"""References in a proposed automation, script or scene, read and checked by Core.

``config_references`` runs Core's own validator on the proposed config (the
automation or script loader's ``_async_validate_config_item``, which also
applies a blueprint's inputs; the scene integration's ``STATES_SCHEMA``), then
collects its references the way Core's entities do: ``Script``'s
``_find_referenced_*`` over the actions and the trigger and condition helpers'
``async_extract_entities`` / ``async_extract_devices`` / ``async_extract_targets``.
A reference is missing when Core's state machine and registries hold no such
entity, device, area, floor or label, or Core has no such service.

Each entity target is also put to Core's ``async_get_services_for_target`` /
``async_get_triggers_for_target`` / ``async_get_conditions_for_target``, the
lookups behind the editor's target pickers (#2989): a config written as YAML or
through the API skips the picker, and Core saves it. That pairing needs one
visit per step; Core's ``determine_script_action`` says which steps are service
calls, and only items whose Core description declares a ``target`` are judged,
since Core's lookup never lists the others.

A config Core's validator refuses answers ``status: "invalid"`` with Core's
error; a Core without one of these parts answers ``status: "unavailable"``.
"""

from __future__ import annotations

import copy
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from .automation_config import _unavailable
from .helper_collections import _INVALID_ERRORS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

WS_CONFIG_REFERENCES = "ha_mcp_tools/config_references"
CAPABILITY = "config_references"
_KINDS = ("automation", "script", "scene")


def _core() -> SimpleNamespace | str:
    """Core's parts this bridge uses, or the names of those this Core lacks."""
    try:
        from homeassistant import const
        from homeassistant.components.automation import config as automation_config
        from homeassistant.components.homeassistant import scene as ha_scene
        from homeassistant.components.script import config as script_config
        from homeassistant.components.websocket_api import automation as ws_automation
        from homeassistant.helpers import (
            area_registry,
            condition,
            device_registry,
            entity_registry,
            floor_registry,
            label_registry,
            service,
            target,
            template,
            trigger,
        )
        from homeassistant.helpers import config_validation as cv
        from homeassistant.helpers.script import Script
    except ImportError as err:
        return str(err.name or err)
    parts = SimpleNamespace(
        const=const,
        # Private, but they are what Core's loaders run; the public ones raise
        # instead of reporting the failure.
        validate_automation=getattr(
            automation_config, "_async_validate_config_item", None
        ),
        validate_script=getattr(script_config, "_async_validate_config_item", None),
        scene_states=getattr(ha_scene, "STATES_SCHEMA", None),
        # Private, but they are how Core's Script lists what its steps reference.
        script_entities=getattr(Script, "_find_referenced_entities", None),
        script_devices=getattr(Script, "_find_referenced_devices", None),
        script_targets=getattr(Script, "_find_referenced_target", None),
        trigger=SimpleNamespace(
            entities=getattr(trigger, "async_extract_entities", None),
            devices=getattr(trigger, "async_extract_devices", None),
            targets=getattr(trigger, "async_extract_targets", None),
        ),
        condition=SimpleNamespace(
            entities=getattr(condition, "async_extract_entities", None),
            devices=getattr(condition, "async_extract_devices", None),
            targets=getattr(condition, "async_extract_targets", None),
        ),
        registries=SimpleNamespace(
            entity=entity_registry.async_get,
            device=device_registry.async_get,
            area=area_registry.async_get,
            floor=floor_registry.async_get,
            label=label_registry.async_get,
        ),
        determine_action=getattr(cv, "determine_script_action", None),
        call_service=getattr(cv, "SCRIPT_ACTION_CALL_SERVICE", None),
        service_name=getattr(cv, "service", None),
        target_selection=getattr(target, "TargetSelection", None),
        referenced=getattr(target, "async_extract_referenced_entity_ids", None),
        is_template=getattr(template, "is_template_string", None),
        descriptions={
            "services": getattr(service, "async_get_all_descriptions", None),
            "triggers": getattr(trigger, "async_get_all_descriptions", None),
            "conditions": getattr(condition, "async_get_all_descriptions", None),
        },
        for_target={
            "services": getattr(ws_automation, "async_get_services_for_target", None),
            "triggers": getattr(ws_automation, "async_get_triggers_for_target", None),
            "conditions": getattr(
                ws_automation, "async_get_conditions_for_target", None
            ),
        },
    )
    missing = [
        f"{name}.{sub}" if sub else name
        for name, value in vars(parts).items()
        for sub, part in (
            value.items()
            if isinstance(value, dict)
            else vars(value).items()
            if isinstance(value, SimpleNamespace)
            else [("", value)]
        )
        if part is None
    ]
    return ", ".join(missing) if missing else parts


async def _validated(
    hass: HomeAssistant, core: SimpleNamespace, kind: str, config: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """The proposed config's triggers, conditions, actions and scene entities
    as Core validates them, or Core's error."""
    const = core.const
    proposed = copy.deepcopy(config)
    if kind == "scene":
        try:
            states = core.scene_states(proposed.get(const.CONF_ENTITIES, {}))
        except _INVALID_ERRORS as err:
            return {}, str(err)
        return {"scene": list(states)}, None
    if kind == "automation":
        checked = await core.validate_automation(
            hass, proposed, raise_on_errors=False, warn_on_errors=False
        )
        actions_key = const.CONF_ACTIONS
    else:
        checked = await core.validate_script(
            hass, "proposed", proposed, raise_on_errors=False, warn_on_errors=False
        )
        actions_key = const.CONF_SEQUENCE
    if str(checked.validation_status) != "ok":
        return {}, checked.validation_error
    triggers = list(checked.get(const.CONF_TRIGGERS) or [])
    conditions = list(checked.get(const.CONF_CONDITIONS) or [])
    actions = list(checked.get(actions_key) or [])
    # Paths follow Core's validated form (plural root keys, ``action`` for
    # ``service``), which is what the get tools return.
    if kind == "automation":
        tree = {
            const.CONF_TRIGGERS: triggers,
            const.CONF_CONDITIONS: conditions,
            actions_key: actions,
        }
    else:
        tree = {actions_key: actions}
    return {
        "triggers": triggers,
        "conditions": conditions,
        "actions": actions,
        "tree": tree,
    }, None


def _references(core: SimpleNamespace, parts: dict[str, Any]) -> dict[str, set]:
    """What the config references, collected as Core's automation entity does."""
    const = core.const
    actions = parts["actions"]
    conditions = parts["conditions"]
    triggers = parts["triggers"]
    found: dict[str, set] = {"entity": set(), "device": set()}
    core.script_entities(found["entity"], actions)
    core.script_devices(found["device"], actions)
    for conf in conditions:
        found["entity"] |= set(core.condition.entities(conf))
        found["device"] |= set(core.condition.devices(conf))
    for conf in triggers:
        found["entity"] |= set(core.trigger.entities(conf))
        found["device"] |= set(core.trigger.devices(conf))
    for kind, attr in (
        ("area", const.ATTR_AREA_ID),
        ("floor", const.ATTR_FLOOR_ID),
        ("label", const.ATTR_LABEL_ID),
    ):
        found[kind] = set()
        core.script_targets(attr, found[kind], actions)
        for conf in conditions:
            found[kind] |= set(core.condition.targets(conf, attr))
        for conf in triggers:
            found[kind] |= set(core.trigger.targets(conf, attr))
    return found


def _missing(
    hass: HomeAssistant, core: SimpleNamespace, found: dict[str, set]
) -> list[dict[str, str]]:
    regs = core.registries
    sentinels = {core.const.ENTITY_MATCH_ALL, core.const.ENTITY_MATCH_NONE}
    exists = {
        "entity": lambda v: (
            hass.states.get(v) is not None or regs.entity(hass).async_get(v) is not None
        ),
        "device": lambda v: regs.device(hass).async_get(v) is not None,
        "area": lambda v: regs.area(hass).async_get_area(v) is not None,
        "floor": lambda v: regs.floor(hass).async_get_floor(v) is not None,
        "label": lambda v: regs.label(hass).async_get_label(v) is not None,
        "service": lambda v: hass.services.has_service(*v.split(".", 1)),
    }
    return [
        {"kind": kind, "value": value}
        for kind, values in found.items()
        for value in sorted(values)
        if isinstance(value, str) and value not in sentinels and not exists[kind](value)
    ]


class _Steps:
    """One visit per item: service names, and entity targets to put to Core."""

    def __init__(
        self, hass: HomeAssistant, core: SimpleNamespace, targeted: dict[str, set]
    ) -> None:
        self.hass = hass
        self.core = core
        self.targeted = targeted
        self.services: set[str] = set()
        self.targets: list[dict[str, Any]] = []

    def _targeted_item(self, node: dict[str, Any], path: str) -> bool:
        """A trigger or condition Core describes with a target; True if one."""
        const = self.core.const
        target = node.get(const.CONF_TARGET)
        if not isinstance(target, dict):
            return False
        for kind, keys in (
            ("triggers", (const.CONF_TRIGGER, const.CONF_PLATFORM)),
            ("conditions", (const.CONF_CONDITION,)),
        ):
            name = next((node[k] for k in keys if isinstance(node.get(k), str)), None)
            if not isinstance(name, str) or name not in self.targeted[kind]:
                continue
            selected = self.core.referenced(
                self.hass, self.core.target_selection(target), expand_group=False
            )
            literal = {
                e
                for e in selected.referenced
                if isinstance(e, str) and not self.core.is_template(e)
            }
            self._add(kind, name, literal, path)
            return True
        return False

    def _add(self, kind: str, name: str, entity_ids: set[str], path: str) -> None:
        for entity_id in sorted(entity_ids):
            # An entity Core has no state for is reported under ``missing``;
            # Core's lookup would offer nothing for it.
            if self.hass.states.get(entity_id) is not None:
                self.targets.append(
                    {"path": path, "kind": kind, "name": name, "entity_id": entity_id}
                )

    def walk(self, node: Any, path: str, listed: bool = False) -> None:
        if isinstance(node, list):
            for i, child in enumerate(node):
                self.walk(child, f"{path}[{i}]", listed=True)
            return
        if not isinstance(node, dict):
            return
        if not listed:
            # Core's validated config keeps every step, trigger and condition
            # in a list; a dict held directly under a key is a payload (an
            # event's data, variables), never one of them.
            for key, value in node.items():
                self.walk(value, f"{path}.{key}" if path else key)
            return
        if self._targeted_item(node, path):
            return
        try:
            action = self.core.determine_action(node)
        except ValueError:
            action = None
        if action == self.core.call_service:
            # A service step holds no further steps; its data is the service's.
            self._service_step(node, path)
            return
        for key, value in node.items():
            self.walk(value, f"{path}.{key}" if path else key)

    def _service_step(self, node: dict[str, Any], path: str) -> None:
        name = node.get(self.core.const.CONF_ACTION)
        if not isinstance(name, str):
            return  # a templated action names no service until it runs
        try:
            # Core's own service-name check: an event's ``action`` data (a
            # mobile app notification action id) is no service call.
            self.core.service_name(name)
        except _INVALID_ERRORS:
            return
        self.services.add(name)
        if name in self.targeted["services"]:
            found: set[str] = set()
            self.core.script_entities(found, [node])
            self._add("services", name, found, path)


def _flat(kind: str, descriptions: Any) -> dict[str, Any]:
    """Descriptions keyed by full name; Core nests services under their domain."""
    if not isinstance(descriptions, dict):
        return {}
    if kind != "services":
        return descriptions
    return {
        f"{domain}.{name}": description
        for domain, services in descriptions.items()
        if isinstance(services, dict)
        for name, description in services.items()
    }


async def _mismatches(
    hass: HomeAssistant, core: SimpleNamespace, steps: _Steps
) -> list[dict[str, Any]]:
    offered: dict[tuple[str, str], set[str]] = {}
    mismatches: list[dict[str, Any]] = []
    for item in steps.targets:
        key = (item["kind"], item["entity_id"])
        if key not in offered:
            offered[key] = set(
                await core.for_target[item["kind"]](
                    hass,
                    {"entity_id": [item["entity_id"]]},
                    # A service call expands a group target to its members.
                    expand_group=True,
                )
            )
        if item["name"] not in offered[key]:
            mismatches.append({**item, "offered": sorted(offered[key])})
    return mismatches


async def _targeted(hass: HomeAssistant, core: SimpleNamespace) -> dict[str, set]:
    """Names of the services, triggers and conditions Core describes with a target."""
    targeted: dict[str, set] = {}
    for kind, describe in core.descriptions.items():
        targeted[kind] = {
            name
            for name, description in _flat(kind, await describe(hass)).items()
            if isinstance(description, dict) and "target" in description
        }
    return targeted


async def async_config_references(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    core = _core()
    if isinstance(core, str):
        _LOGGER.warning("%s: this Home Assistant lacks %s", WS_CONFIG_REFERENCES, core)
        return _unavailable(
            f"this Home Assistant lacks {core}, which the reference check needs"
        )
    try:
        parts, error = await _validated(hass, core, msg["kind"], msg["config"])
        if error is not None:
            return {"status": "invalid", "error": error}
        if msg["kind"] == "scene":
            found = {"entity": set(parts["scene"])}
            mismatches: list[dict[str, Any]] = []
        else:
            found = _references(core, parts)
            steps = _Steps(hass, core, await _targeted(hass, core))
            steps.walk(parts["tree"], "")
            found["service"] = steps.services
            mismatches = await _mismatches(hass, core, steps)
        return {
            "status": "ok",
            "missing": _missing(hass, core, found),
            "mismatches": mismatches,
        }
    except Exception as err:
        _LOGGER.warning(
            "%s failed on a %s config", WS_CONFIG_REFERENCES, msg["kind"], exc_info=True
        )
        return _unavailable(
            f"the reference check failed ({type(err).__name__}: {err}); "
            "see the Home Assistant log"
        )


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    """The (schema, handler, async prep) row websocket_api registers."""

    async def prep(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
        return {"result": await async_config_references(hass, msg)}

    def do_config_references(
        hass: HomeAssistant, msg: dict[str, Any], *, result: dict[str, Any]
    ) -> dict[str, Any]:
        return result

    return [
        (
            {
                vol.Required("type"): WS_CONFIG_REFERENCES,
                vol.Required("kind"): vol.In(_KINDS),
                vol.Required("config"): dict,
            },
            do_config_references,
            prep,
        )
    ]
