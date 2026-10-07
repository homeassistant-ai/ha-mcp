"""Describe and validate selected Core commands without invoking their handlers.

The registry is the authority: this bridge holds no copies of Core validators.
It never dispatches a command. Writes still use Core's authenticated endpoints.
"""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING, Any

from .helper_collections import _INVALID_ERRORS, _convert, _optional_attr

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
_UNSUPPORTED = _optional_attr("probatio", "UNSUPPORTED")
_TO_JSON_SCHEMA = _optional_attr("probatio", "to_json_schema")
COMMAND = "ha_mcp_tools/core_contract"
CAPABILITY = "core_contract"
# This is a bridge permission boundary, not a copy of the commands' fields.
COMMANDS = (
    "energy/save_prefs",
    "history/history_during_period",
    "recorder/statistics_during_period",
)


def _schema(hass: HomeAssistant, command: str) -> Any:
    if command not in COMMANDS:
        raise ValueError("Unsupported Core contract")
    entry = (hass.data.get("websocket_api") or {}).get(command)
    return entry[1] if entry else None


def validate_request(
    hass: HomeAssistant, command: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Call the exact installed validator, including its defaults and coercions."""
    if {"id", "type"} & payload.keys():
        raise ValueError("id and type are reserved transport fields")
    schema = _schema(hass, command)
    if schema is None:
        return {"status": "unavailable", "reason": "Core command is not registered"}
    try:
        schema({**payload, "id": 1, "type": command})
    except _INVALID_ERRORS as err:
        return {
            "status": "validated",
            "valid": False,
            "errors": [
                {"path": list(getattr(issue, "path", [])), "message": str(issue)}
                for issue in (getattr(err, "errors", None) or [err])
            ],
        }
    return {"status": "validated", "valid": True, "errors": []}


def _core_serializer(node: Any) -> Any:
    """Describe Core's opaque discriminated unions from their live schema objects.

    cv.key_value_schemas closes over its alternatives instead of publishing a
    serializer. This adapter only reveals those objects; it never validates or
    supplies a list of source types/fields. Unknown wrappers remain unsupported.
    """
    if not inspect.isfunction(node) or node.__module__ != "homeassistant.helpers.config_validation":
        return _UNSUPPORTED
    if node.__qualname__ != "key_value_schemas.<locals>.key_value_validator":
        return _UNSUPPORTED
    closure = inspect.getclosurevars(node).nonlocals
    alternatives = closure.get("value_schemas")
    if not isinstance(alternatives, dict) or closure.get("default_schema") is not None:
        return _UNSUPPORTED
    return {"anyOf": [
        _TO_JSON_SCHEMA(schema, custom_serializer=_core_serializer)
        for schema in alternatives.values()
    ]}


def describe_contract(hass: HomeAssistant, command: str) -> dict[str, Any]:
    """Use Core's serializer; never present a lossy description as validation."""
    schema = _schema(hass, command)
    if schema is None:
        return {"status": "unavailable", "reason": "Core command is not registered"}
    result: dict[str, Any] = {"status": "available", "command": command}
    try:
        if _TO_JSON_SCHEMA is not None:
            try:
                result["schema"] = _TO_JSON_SCHEMA(schema, strict=True, custom_serializer=_core_serializer)
                result["description_complete"] = False
            except Exception:
                _LOGGER.debug("Core contract needs lossy serialization", exc_info=True)
                result["schema"] = _TO_JSON_SCHEMA(schema, custom_serializer=_core_serializer)
                result["description_complete"] = False
        else:
            result["fields"] = _convert(schema)
            result["description_complete"] = False
    except Exception:
        _LOGGER.debug("Core contract cannot be serialized", exc_info=True)
        result["description_complete"] = False
    if not result["description_complete"]:
        result["warnings"] = [
            "Some Core validators cannot be described. Validation still uses the complete registered schema."
        ]
    return result


def _defaults() -> dict[str, Any]:
    from homeassistant.components.energy.data import EnergyManager

    return dict(EnergyManager.default_preferences())


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    """Register on the shared admin-gated surface for both component entries."""
    def execute(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
        command = msg["command"]
        if "payload" in msg:
            return validate_request(hass, command, msg["payload"])
        result = describe_contract(hass, command)
        if command == "energy/save_prefs":
            result["default_preferences"] = _defaults()
        return result

    async def units_prep(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
        return {"result": await statistics_metadata(hass, msg)}

    def units_result(hass: HomeAssistant, msg: dict[str, Any], *, result: dict[str, Any]) -> dict[str, Any]:
        return result

    return [(
        {
            vol.Required("type"): COMMAND,
            vol.Required("command"): vol.In(COMMANDS),
            vol.Optional("payload"): dict,
        },
        execute,
        None,
    ), (
        {
            vol.Required("type"): "ha_mcp_tools/statistics_units",
            vol.Required("statistic_ids"): vol.All([str], vol.Length(min=1)),
            vol.Required("units"): {str: str},
        },
        units_result,
        units_prep,
    )]


async def statistics_metadata(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    """Resolve explicit-unit labels with Core's converter selection, not a unit table."""
    from homeassistant.components.recorder.statistics import (
        _get_unit_converter,
        async_list_statistic_ids,
    )

    records = await async_list_statistic_ids(hass, set(msg["statistic_ids"]))
    units = msg["units"]
    for record in records:
        stored = record.get("statistics_unit_of_measurement")
        converter = _get_unit_converter(record.get("unit_class"), stored)
        requested = units.get(converter.UNIT_CLASS) if converter else None
        # A converter absent in Core means statistics are returned as stored.
        output = stored if converter is None else record.get("display_unit_of_measurement")
        if converter and requested is not None:
            output = requested if requested in converter.VALID_UNITS else stored
        record["output_unit_of_measurement"] = output
    return {"records": records}
