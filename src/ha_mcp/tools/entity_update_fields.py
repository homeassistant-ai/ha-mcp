"""Builders for the config/entity_registry/update message of ha_set_entity."""

from typing import Any

from .helpers import clearable_value


def build_name_visibility_fields(
    message: dict[str, Any],
    updates_made: list[str],
    area_id: str | None,
    name: str | None,
    icon: str | None,
    device_class: str | None,
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
