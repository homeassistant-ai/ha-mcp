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

from .helper_collections import _INVALID_ERRORS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

WS_CONFIG_REFERENCES = "ha_mcp_tools/config_references"
CAPABILITY = "config_references"
_KINDS = ("automation", "script", "scene")


def _unavailable(error: str) -> dict[str, Any]:
    return {"status": "unavailable", "error": error}


def _core() -> SimpleNamespace | None:
    """Core's parts this bridge uses, or None on a Core without them."""
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
    except ImportError:
        return None
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
        trigger=trigger,
        condition=condition,
        registries=SimpleNamespace(
            entity=entity_registry.async_get,
            device=device_registry.async_get,
            area=area_registry.async_get,
            floor=floor_registry.async_get,
            label=label_registry.async_get,
        ),
        determine_action=getattr(cv, "determine_script_action", None),
        call_service=getattr(cv, "SCRIPT_ACTION_CALL_SERVICE", None),
        service_schema=getattr(cv, "SERVICE_SCHEMA", None),
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
        name
        for name, value in vars(parts).items()
        if value is None
        or (isinstance(value, dict) and any(v is None for v in value.values()))
    ]
    return None if missing else parts


async def _validated(
    hass: HomeAssistant, core: SimpleNamespace, kind: str, config: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """The proposed config's triggers, conditions, actions and scene entities
    as Core validates them, or Core's error."""
    const = core.const
    proposed = copy.deepcopy(config)
    if kind == "scene":
        try:
            states = core.scene_states(proposed.get(const.CONF_ENTITIES) or {})
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
    tree = {actions_key: actions}
    if kind == "automation":
        tree = {
            const.CONF_TRIGGERS: triggers,
            const.CONF_CONDITIONS: conditions,
            **tree,
        }
    return {
        "triggers": triggers,
        "conditions": conditions,
        "actions": actions,
        # The validated config as written, for the paths reported.
        "tree": tree,
    }, None


def _references(core: SimpleNamespace, parts: dict[str, Any]) -> dict[str, set]:
    """What the config references, collected as Core's automation entity does."""
    const = core.const
    if "scene" in parts:
        return {"entity": set(parts["scene"])}
    actions, conditions, triggers = (
        parts["actions"],
        parts["conditions"],
        parts["triggers"],
    )
    found: dict[str, set] = {"entity": set(), "device": set()}
    core.script_entities(found["entity"], actions)
    core.script_devices(found["device"], actions)
    for conf in conditions:
        found["entity"] |= core.condition.async_extract_entities(conf)
        found["device"] |= core.condition.async_extract_devices(conf)
    for conf in triggers:
        found["entity"] |= set(core.trigger.async_extract_entities(conf))
        found["device"] |= set(core.trigger.async_extract_devices(conf))
    for kind, attr in (
        ("area", const.ATTR_AREA_ID),
        ("floor", const.ATTR_FLOOR_ID),
        ("label", const.ATTR_LABEL_ID),
    ):
        found[kind] = set()
        core.script_targets(attr, found[kind], actions)
        for conf in conditions:
            found[kind] |= core.condition.async_extract_targets(conf, attr)
        for conf in triggers:
            found[kind] |= set(core.trigger.async_extract_targets(conf, attr))
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
        self.items: list[dict[str, Any]] = []

    def _targeted_item(self, node: dict[str, Any], path: str) -> bool:
        """A trigger or condition Core describes with a target; True if one."""
        const = self.core.const
        target = node.get(const.CONF_TARGET)
        for kind, keys in (
            ("triggers", (const.CONF_TRIGGER, const.CONF_PLATFORM)),
            ("conditions", (const.CONF_CONDITION,)),
        ):
            name = next((node[k] for k in keys if isinstance(node.get(k), str)), None)
            if (
                not isinstance(name, str)
                or name not in self.targeted[kind]
                or not isinstance(target, dict)
            ):
                continue
            selected = self.core.referenced(
                self.hass, self.core.target_selection(target), expand_group=False
            )
            self._add(
                kind,
                name,
                {e for e in selected.referenced if not self.core.is_template(e)},
                path,
            )
            return True
        return False

    def _add(self, kind: str, name: str, entity_ids: set[str], path: str) -> None:
        for entity_id in sorted(entity_ids):
            if self.hass.states.get(entity_id) is not None:
                self.items.append(
                    {"path": path, "kind": kind, "name": name, "entity_id": entity_id}
                )

    def walk(self, node: Any, path: str) -> None:
        if isinstance(node, list):
            for i, child in enumerate(node):
                self.walk(child, f"{path}[{i}]")
            return
        if not isinstance(node, dict):
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
        try:
            # Core's own service-call schema: an ``action`` key in an event's
            # data or a notification's buttons is no service call.
            self.core.service_schema(copy.deepcopy(node))
        except _INVALID_ERRORS:
            return
        name = node.get(self.core.const.CONF_ACTION)
        if isinstance(name, str):
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
    for item in steps.items:
        key = (item["kind"], item["entity_id"])
        if key not in offered:
            offered[key] = set(
                await core.for_target[item["kind"]](
                    hass, {"entity_id": [item["entity_id"]]}, True
                )
            )
        if item["name"] not in offered[key]:
            mismatches.append({**item, "offered": sorted(offered[key])})
    return mismatches


async def async_config_references(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    core = _core()
    if core is None:
        return _unavailable("This Home Assistant lacks a part this check asks")
    try:
        parts, error = await _validated(hass, core, msg["kind"], msg["config"])
        if error is not None:
            return {"status": "invalid", "error": error}
        found = _references(core, parts)
        if "scene" in parts:
            return {
                "status": "ok",
                "missing": _missing(hass, core, found),
                "mismatches": [],
            }
        targeted = {
            kind: {
                name
                for name, description in _flat(kind, await describe(hass)).items()
                if isinstance(description, dict) and "target" in description
            }
            for kind, describe in core.descriptions.items()
        }
        steps = _Steps(hass, core, targeted)
        steps.walk(parts["tree"], "")
        found["service"] = steps.services
        return {
            "status": "ok",
            "missing": _missing(hass, core, found),
            "mismatches": await _mismatches(hass, core, steps),
        }
    except Exception as err:
        _LOGGER.warning("%s failed", WS_CONFIG_REFERENCES, exc_info=True)
        return _unavailable(str(err) or type(err).__name__)


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
