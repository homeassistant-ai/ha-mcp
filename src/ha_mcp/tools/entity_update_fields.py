"""Builders for the config/entity_registry/update message of ha_set_entity."""

from collections.abc import Awaitable, Callable
from typing import Any

from ..errors import ErrorCode, create_entity_not_found_error, create_error_response
from .entity_registry_reads import NotInRegistry
from .helpers import clearable_value, raise_core_refusal, raise_tool_error


async def automatic_entity_id(
    client: Any, entity_id: str, name: str | None, area_id: str | None
) -> str:
    """The entity_id Core would generate for the entry (the UI's restore),
    with the call's unsaved ``name`` and ``area_id`` as Core 2026.12's
    ``overrides``; a Core that refuses them answers for the stored ones."""
    request: dict[str, Any] = {
        "type": "config/entity_registry/get_automatic_entity_ids",
        "entity_ids": [entity_id],
    }
    overrides = {
        key: clearable_value(value, key)
        for key, value in (("name", name), ("area_id", area_id))
        if value is not None
    }
    result = await client.send_websocket_message(
        {**request, "overrides": {entity_id: overrides}} if overrides else request,
        expected_error_codes=frozenset({"invalid_format"}),
    )
    if overrides and result.get("error_code") == "invalid_format":
        result = await client.send_websocket_message(request)
    what = f"No automatic entity_id for {entity_id}"
    context = {"entity_id": entity_id}
    if not result.get("success"):
        raise_core_refusal(what, result, context)
    # Core lowercases the ids it is sent and keys its reply so.
    automatic = (result.get("result") or {}).get(entity_id.lower())
    if not automatic:
        raise_tool_error(
            create_error_response(
                ErrorCode.ENTITY_NOT_FOUND,
                f"{what}: not in the entity registry",
                context=context,
                suggestions=["Use ha_search() to find valid entity IDs"],
            )
        )
    return str(automatic)


async def reject_unconvertible_unit(
    client: Any,
    entity_id: str,
    options: dict[str, dict[str, Any]] | None,
    device_class: str | None,
    fetch_entity: Callable[[str], Awaitable[dict[str, Any]]],
) -> None:
    """Refuse a sensor unit Core does not list for the sensor's device class.

    Core stores any unit in the sensor options and then ignores one it cannot
    convert to; the units come from ``sensor/device_class_convertible_units``,
    the list the entity settings dialog offers. The device class is the one
    this call sets, else the entry's own, else the integration's.
    """
    unit = ((options or {}).get("sensor") or {}).get("unit_of_measurement")
    if unit is None or not entity_id.lower().startswith("sensor."):
        return
    effective = clearable_value(device_class, "device_class")
    if effective is None:
        try:
            entry = await fetch_entity(entity_id)
        except NotInRegistry:
            return  # Core refuses options for an entity it does not register
        effective = entry.get("device_class") or entry.get("original_device_class")
    units: list[str] = []
    if effective:
        reply = await client.send_websocket_message(
            {"type": "sensor/device_class_convertible_units", "device_class": effective}
        )
        if not reply.get("success"):
            return  # unchecked: Core stores the unit as before
        units = (reply.get("result") or {}).get("units") or []
    if unit in units:
        return
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"Home Assistant does not convert {entity_id} "
            f"(device class {effective or 'none'}) to {unit!r}: it keeps the "
            "unit option but ignores it",
            context={
                "entity_id": entity_id,
                "unit_of_measurement": unit,
                "device_class": effective,
                "convertible_units": units,
            },
            suggestions=[
                f"Use one of: {', '.join(units)}"
                if units
                else "Home Assistant lists no units this sensor converts to"
            ],
        )
    )


def reject_name_with_use_device_name(
    name: str | None, use_device_name: bool | None
) -> None:
    """Both write the same registry field with different meanings."""
    if name is not None and use_device_name is not None:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "name and use_device_name cannot be combined: use_device_name "
                "replaces the name override with the device name",
                suggestions=[
                    "Pass use_device_name=True alone to follow the device name",
                    "Pass name alone to set a custom name ('' reverts to the default)",
                ],
            )
        )


def reject_use_device_name_without_device(
    entity_id: str, entry: dict[str, Any]
) -> None:
    """A device-less entity has no device name; Core would render an empty name."""
    if entry.get("device_id"):
        return
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"{entity_id} has no device, so there is no device name to follow",
            context={"entity_id": entity_id},
            suggestions=["Pass name='<text>' to give the entity a custom name"],
        )
    )


async def ensure_use_device_name_allowed(
    use_device_name: bool | None,
    entity_id: str,
    fetch_entity: Callable[[str], Awaitable[dict[str, Any]]],
) -> dict[str, Any] | None:
    """use_device_name=True needs an entity with a device.

    Returns the entry it read for use_device_name=True (the main-entity
    write is built from it), else None.
    """
    if not use_device_name:
        return None
    try:
        current = await fetch_entity(entity_id)
    except NotInRegistry:
        raise_tool_error(
            create_entity_not_found_error(
                entity_id, details=f"{entity_id} has no entity registry entry."
            )
        )
    reject_use_device_name_without_device(entity_id, current)
    return current


def is_main_entity(entry: dict[str, Any]) -> bool:
    """An entity with no name of its own is the main entity of its device."""
    own_name = entry.get("name")
    if own_name is None:
        own_name = entry.get("original_name")
    return not own_name


def main_entity_fields(entry: dict[str, Any]) -> dict[str, Any]:
    """What the entity dialog's "Main entity of the device" switch writes.

    The main entity gets its unchanged name back, as the dialog sends it, so
    Core changes nothing and returns the entry. Otherwise "" overrides an
    integration name (None is enough without one) and the entity leaves its
    own area for the device's.
    """
    if is_main_entity(entry):
        return {"name": entry.get("name")}
    fields: dict[str, Any] = {"name": "" if entry.get("original_name") else None}
    if entry.get("area_id"):
        fields["area_id"] = None
    return fields


def build_name_visibility_fields(
    message: dict[str, Any],
    updates_made: list[str],
    area_id: str | None,
    name: str | None,
    icon: str | None,
    device_class: str | None,
    use_device_name: bool | None = None,
    entry: dict[str, Any] | None = None,
) -> None:
    """Add basic positioning/appearance fields to the update message.

    ``entry`` is the current registry entry, read for use_device_name=True.
    """
    if area_id is not None:
        area_id = clearable_value(area_id, "area_id")
        message["area_id"] = area_id
        updates_made.append(f"area_id='{area_id}'" if area_id else "area cleared")
    if name is not None:
        name = clearable_value(name, "name")
        message["name"] = name
        updates_made.append(f"name='{name}'" if name else "name cleared")
    if use_device_name is False:
        message["name"] = None
        updates_made.append("name cleared")
    elif use_device_name:
        assert entry is not None  # read by ensure_use_device_name_allowed
        already_main = is_main_entity(entry)
        fields = main_entity_fields(entry)
        # An explicit area_id goes to Core, which refuses it on a main entity
        if area_id is not None:
            fields.pop("area_id", None)
        message.update(fields)
        updates_made.append(
            "already the main entity" if already_main else "main entity of the device"
        )
        if "area_id" in fields:
            updates_made.append("area cleared: follows the device's area")
    if icon is not None:
        icon = clearable_value(icon, "icon")
        message["icon"] = icon
        updates_made.append(f"icon='{icon}'" if icon else "icon cleared")
    if device_class is not None:
        device_class = clearable_value(device_class, "device_class")
        message["device_class"] = device_class
        updates_made.append(
            f"device_class='{device_class}'" if device_class else "device_class cleared"
        )


def build_state_tag_fields(
    message: dict[str, Any],
    updates_made: list[str],
    enabled: bool | None,
    hidden: bool | None,
    parsed_aliases: list[str | None] | None,
    parsed_categories: dict[str, str | None] | None,
    final_labels: list[str] | None,
    label_operation: str,
    parsed_labels: list[str] | None,
) -> None:
    """Add enabled/hidden/alias/category/label fields to the update message."""
    if enabled is not None:
        message["disabled_by"] = None if enabled else "user"
        updates_made.append("enabled" if enabled else "disabled")
    if hidden is not None:
        message["hidden_by"] = "user" if hidden else None
        updates_made.append("hidden" if hidden else "visible")
    if parsed_aliases is not None:
        message["aliases"] = parsed_aliases
        updates_made.append(f"aliases={parsed_aliases}")
    if parsed_categories is not None:
        message["categories"] = parsed_categories
        updates_made.append(f"categories={parsed_categories}")
    if final_labels is not None:
        message["labels"] = final_labels
        if label_operation == "set":
            updates_made.append(f"labels={final_labels}")
        elif label_operation == "add":
            updates_made.append(f"labels added: {parsed_labels} -> {final_labels}")
        else:  # remove
            updates_made.append(f"labels removed: {parsed_labels} -> {final_labels}")
