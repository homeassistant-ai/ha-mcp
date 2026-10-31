"""Automation configs validated, described and looked up by Core in process.

Core validates an automation with ``_async_validate_config_item`` and keeps the
outcome as a ``validation_status`` and a humanized ``validation_error``; an
automation that fails to load shows that same pair in its repair issue. The
``validate`` action runs that function on a proposed config, so the server
needs no copy of Core's automation schema. ``describe`` answers Core's trigger
or condition descriptions, keyed by every registered platform. ``lookup``
joins an automation's entity registry row, the validation repair issue Core
filed for it and the references Core's search integration reads.

A Core without one of these parts, or one that fails unexpectedly, answers
``status: "unavailable"`` and the server takes its REST/WebSocket fallback.
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

WS_AUTOMATION_CONFIG = "ha_mcp_tools/automation_config"
# Advertised by websocket_api's info; the server's automation tools use it and
# fall back to Core's automation REST API and WebSocket commands.
CAPABILITY = "automation_config"
_DOMAIN = "automation"
# The field each action reads; lookup takes either key.
_NEEDS: dict[str, tuple[str, ...]] = {
    "validate": ("config",),
    "describe": ("kind",),
    "lookup": ("entity_id", "unique_id"),
}
_KINDS = ("trigger", "condition")
_REFERENCES = ("entities", "devices", "areas", "floors", "labels")


def _unavailable(error: str) -> dict[str, Any]:
    return {"status": "unavailable", "error": error}


def _core() -> SimpleNamespace | None:
    """Core's automation parts this bridge uses, or None on a Core without them.

    A part a Core build lacks is None; the action that needs it answers
    unavailable, the others still run.
    """
    try:
        from homeassistant.components import automation
        from homeassistant.components.automation import config, const
        from homeassistant.exceptions import HomeAssistantError
        from homeassistant.helpers import condition, trigger
        from homeassistant.helpers import entity_registry as er
        from homeassistant.helpers import issue_registry as ir
    except ImportError:
        return None
    return SimpleNamespace(
        # Private, but it is what Core's loader runs and what fills the repair
        # issue; the public one raises instead of reporting the failed section.
        validate=getattr(config, "_async_validate_config_item", None),
        validate_or_raise=getattr(config, "async_validate_config_item", None),
        invalid=(
            # Core reports a bad config as Invalid or HomeAssistantError; a
            # ValueError is an unexpected failure, answered unavailable.
            *(e for e in _INVALID_ERRORS if e is not ValueError),
            HomeAssistantError,
        ),
        statuses=getattr(config, "ValidationStatus", None),
        descriptions={
            "trigger": getattr(trigger, "async_get_all_descriptions", None),
            "condition": getattr(condition, "async_get_all_descriptions", None),
        },
        entity_registry=getattr(er, "async_get", None),
        issue_registry=getattr(ir, "async_get", None),
        # Defined on the package through Core 2026.10 and in const from 2026.11.
        component_key=getattr(automation, "DATA_COMPONENT", None)
        or getattr(const, "DATA_COMPONENT", None),
        references={
            name: getattr(automation, f"{name}_in_automation", None)
            for name in _REFERENCES
        },
        blueprint=getattr(automation, "blueprint_in_automation", None),
        # The placeholder Core adds for an automation that failed validation;
        # its reference readers answer empty sets.
        unavailable_entity=getattr(automation, "UnavailableAutomationEntity", None),
    )


async def _validate(
    hass: HomeAssistant, core: SimpleNamespace, msg: dict[str, Any]
) -> dict[str, Any]:
    # Core's validators change the dict they are given.
    proposed = copy.deepcopy(msg["config"])
    try:
        if core.validate is not None:
            checked = await core.validate(
                hass, proposed, raise_on_errors=False, warn_on_errors=False
            )
        elif core.validate_or_raise is not None:
            checked = await core.validate_or_raise(hass, "", proposed)
        else:
            return _unavailable("This Home Assistant has no automation validator")
    except core.invalid as err:
        # Raised by the public validator on any failure, and by the private one
        # when even the id/alias/description check fails.
        return {"status": "failed_schema", "error": str(err)}
    status = getattr(checked, "validation_status", None)
    if status is None:
        return _unavailable("Home Assistant's automation validator gave no status")
    return {"status": str(status), "error": getattr(checked, "validation_error", None)}


async def _describe(
    hass: HomeAssistant, core: SimpleNamespace, msg: dict[str, Any]
) -> dict[str, Any]:
    describe = core.descriptions[msg["kind"]]
    if describe is None:
        return _unavailable(f"This Home Assistant has no {msg['kind']} descriptions")
    return {"descriptions": await describe(hass)}


def _registry_entry(registry: Any, msg: dict[str, Any]) -> Any:
    if msg.get("entity_id"):
        entry = registry.async_get(msg["entity_id"])
        if entry is not None and entry.platform == _DOMAIN:
            return entry
    if not msg.get("unique_id"):
        return None
    entity_id = registry.async_get_entity_id(_DOMAIN, _DOMAIN, msg["unique_id"])
    return registry.async_get(entity_id) if entity_id else None


def _validation(
    hass: HomeAssistant, core: SimpleNamespace, entity_id: str
) -> dict[str, Any] | None:
    """The validation repair issue Core keeps for an automation it could not load.

    Read by its exact id: an id prefix can match another automation's
    entity_id. An inactive issue is a stored record from an earlier run.
    """
    issues = core.issue_registry(hass)
    for status in core.statuses:
        issue = issues.async_get_issue(_DOMAIN, f"{entity_id}_validation_{status}")
        if issue is not None and issue.active:
            placeholders = issue.translation_placeholders or {}
            return {"status": str(status), "error": placeholders.get("error")}
    return None


def _loaded(hass: HomeAssistant, core: SimpleNamespace, entity_id: str) -> bool:
    """Whether Core's automation EntityComponent holds a working entity for it.

    Core also adds an entity for an automation that failed validation, an
    unavailable placeholder whose reference readers answer empty sets; that
    one does not count as loaded.
    """
    component = hass.data.get(core.component_key)
    entity = component.get_entity(entity_id) if component is not None else None
    return entity is not None and not isinstance(entity, core.unavailable_entity)


def _references(
    hass: HomeAssistant, core: SimpleNamespace, entity_id: str
) -> dict[str, Any]:
    found: dict[str, Any] = {
        name: sorted(read(hass, entity_id)) for name, read in core.references.items()
    }
    found["blueprint"] = core.blueprint(hass, entity_id)
    return found


async def _lookup(
    hass: HomeAssistant, core: SimpleNamespace, msg: dict[str, Any]
) -> dict[str, Any]:
    needed = (
        core.entity_registry,
        core.issue_registry,
        core.statuses,
        core.component_key,
        core.blueprint,
        core.unavailable_entity,
        *core.references.values(),
    )
    if None in needed:
        return _unavailable("This Home Assistant lacks part of the automation lookup")
    entry = _registry_entry(core.entity_registry(hass), msg)
    if entry is None:
        return {
            "found": False,
            "entity": None,
            "loaded": False,
            "validation": None,
            "references": None,
        }
    loaded = _loaded(hass, core, entry.entity_id)
    return {
        "found": True,
        # The row Core's config/entity_registry/get sends.
        "entity": entry.extended_dict,
        "loaded": loaded,
        "validation": _validation(hass, core, entry.entity_id),
        # Core's helpers read the loaded entity; without it they find nothing.
        "references": _references(hass, core, entry.entity_id) if loaded else None,
    }


_HANDLERS = {"validate": _validate, "describe": _describe, "lookup": _lookup}


def _require(msg: dict[str, Any]) -> None:
    """Refuse a request without the field its action reads, as registry_lookup
    does, rather than answer a result the caller could take for "not found"."""
    action = msg["action"]
    needs = _NEEDS[action]
    if all(msg.get(field) in (None, "") for field in needs):
        from homeassistant.exceptions import HomeAssistantError

        raise HomeAssistantError(
            f"{WS_AUTOMATION_CONFIG} {action} needs {' or '.join(needs)}"
        )


async def async_automation_config(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    _require(msg)
    core = _core()
    if core is None:
        return _unavailable("This Home Assistant has no automation integration")
    try:
        return await _HANDLERS[msg["action"]](hass, core, msg)
    except Exception as err:
        # Core drift or a failure in Core: the server takes its fallback, which
        # reaches Core's own API and so its own error.
        _LOGGER.warning(
            "%s %s failed", WS_AUTOMATION_CONFIG, msg["action"], exc_info=True
        )
        return _unavailable(str(err) or type(err).__name__)


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    """The (schema, handler, async prep) row websocket_api registers."""

    async def prep(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
        return {"result": await async_automation_config(hass, msg)}

    def do_automation_config(
        hass: HomeAssistant, msg: dict[str, Any], *, result: dict[str, Any]
    ) -> dict[str, Any]:
        return result

    return [
        (
            {
                vol.Required("type"): WS_AUTOMATION_CONFIG,
                vol.Required("action"): vol.In(tuple(_NEEDS)),
                vol.Optional("config"): dict,
                vol.Optional("kind"): vol.In(_KINDS),
                vol.Optional("entity_id"): str,
                vol.Optional("unique_id"): str,
            },
            do_automation_config,
            prep,
        )
    ]
