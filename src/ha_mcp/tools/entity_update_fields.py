"""Builders for the config/entity_registry/update message of ha_set_entity."""

from collections.abc import Awaitable, Callable
from typing import Any

from packaging.version import InvalidVersion, Version

from ..errors import ErrorCode, create_error_response
from .helpers import clearable_value, raise_tool_error

# Home Assistant release that reads an empty entity-name override as "use the
# device name" (home-assistant/core#181576). Older Cores store "" but render
# the default name, so the switch would silently do the opposite there.
USE_DEVICE_NAME_MIN_CORE = (2026, 10)


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


def uses_device_name(entry: dict[str, Any]) -> bool:
    """An empty name override follows the device name; without a device there is nothing to follow."""
    return entry.get("name") == "" and bool(entry.get("device_id"))


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
    client: Any,
    entity_id: str,
    fetch_entity: Callable[[str], Awaitable[dict[str, Any]]],
) -> None:
    """use_device_name=True needs a 2026.10 Core and an entity with a device."""
    if not use_device_name:
        return
    await ensure_use_device_name_supported(client)
    try:
        current = await fetch_entity(entity_id)
    except ValueError as e:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Entity not found: {e}",
                context={"entity_id": entity_id},
                suggestions=["Use ha_search() to find valid entity IDs"],
            )
        )
    reject_use_device_name_without_device(entity_id, current)


async def ensure_use_device_name_supported(client: Any) -> None:
    """Refuse use_device_name=True on a Core older than 2026.10."""
    config = await client.get_config()
    raw = str(config.get("version", ""))
    try:
        release = Version(raw).release[:2]
    except InvalidVersion:
        release = ()
    if tuple(release) >= USE_DEVICE_NAME_MIN_CORE:
        return
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"use_device_name needs Home Assistant 2026.10 or newer; this Core "
            f"reports {raw or 'an unknown version'} and would show the default "
            f"name instead",
            context={"ha_version": raw},
            suggestions=[
                "Upgrade Home Assistant to 2026.10 or newer",
                "Pass name='<device name>' to set the device's name as a fixed custom name",
            ],
        )
    )


def build_name_visibility_fields(
    message: dict[str, Any],
    updates_made: list[str],
    area_id: str | None,
    name: str | None,
    icon: str | None,
    device_class: str | None,
    use_device_name: bool | None = None,
) -> None:
    """Add basic positioning/appearance fields to the update message."""
    if area_id is not None:
        area_id = clearable_value(area_id, "area_id")
        message["area_id"] = area_id
        updates_made.append(f"area_id='{area_id}'" if area_id else "area cleared")
    if name is not None:
        name = clearable_value(name, "name")
        message["name"] = name
        updates_made.append(f"name='{name}'" if name else "name cleared")
    if use_device_name is not None:
        message["name"] = "" if use_device_name else None
        updates_made.append(
            "name follows device" if use_device_name else "name cleared"
        )
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
