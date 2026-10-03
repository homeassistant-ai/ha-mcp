"""Update path for simple (non-flow) helper types."""

from collections.abc import Callable
from typing import Any

from ...errors import ErrorCode, create_error_response
from ...utils.registry_update_lock import registry_update_lock
from ..component_helper_collections import (
    collection_payload,
    native_result,
    read_helper_item,
    tag_entity_id,
    tag_item_id,
    write_helper_item,
)
from ..helpers import raise_tool_error, ws_failure_code
from ..util_helpers import apply_entity_category, wait_for_entity_registered
from .create import _SCHEDULE_DAYS, _format_schedule_days
from .registry import _ws_error_msg
from .schemas import (
    _attach_helper_skill,
    _helper_response,
    _simple_helper_error_context,
)
from .validation import (
    _validate_datetime_has_date_or_time,
    _validate_initial_in_options,
    _validate_merged_range,
    _validate_mode,
)

# ---------------------------------------------------------------------------
# UPDATE INFRASTRUCTURE
# ---------------------------------------------------------------------------


def _update_fields_input_select(
    existing: dict[str, Any],
    options: list[str] | None,
    initial: Any,
    **_: Any,
) -> dict[str, Any]:
    merged_options = options if options is not None else existing.get("options", [])
    initial_val = initial if initial is not None else existing.get("initial")
    _validate_initial_in_options(
        merged_options, initial_val, stored=initial is None and options is not None
    )
    fields: dict[str, Any] = {"options": merged_options}
    if initial_val is not None:
        fields["initial"] = initial_val
    return fields


def _update_fields_input_number(
    existing: dict[str, Any],
    min_value: float | None,
    max_value: float | None,
    step: float | None,
    unit_of_measurement: str | None,
    mode: str | None,
    initial: Any,
    **_: Any,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "min": min_value if min_value is not None else existing.get("min", 0),
        "max": max_value if max_value is not None else existing.get("max", 100),
    }
    step_val = step if step is not None else existing.get("step")
    if step_val is not None:
        fields["step"] = step_val
    unit_val = (
        unit_of_measurement
        if unit_of_measurement is not None
        else existing.get("unit_of_measurement")
    )
    if unit_val is not None:
        fields["unit_of_measurement"] = unit_val
    _validate_merged_range(
        "input_number",
        (min_value, max_value, step),
        fields["min"],
        fields["max"],
        step_val,
    )
    _validate_mode("input_number", mode)
    mode_val = mode if mode is not None else existing.get("mode")
    if mode_val is not None:
        fields["mode"] = mode_val
    initial_val = initial if initial is not None else existing.get("initial")
    if initial_val is not None:
        fields["initial"] = initial_val
    return fields


def _update_fields_input_text(
    existing: dict[str, Any],
    min_value: float | None,
    max_value: float | None,
    mode: str | None,
    initial: Any,
    unit_of_measurement: str | None = None,
    pattern: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    # Full-replace update: an unpassed unit or pattern keeps its stored value.
    fields: dict[str, Any] = {
        key: value if value is not None else existing.get(key)
        for key, value in (
            ("unit_of_measurement", unit_of_measurement),
            ("pattern", pattern),
        )
        if (value if value is not None else existing.get(key)) is not None
    }
    min_val = int(min_value) if min_value is not None else existing.get("min")
    if min_val is not None:
        fields["min"] = min_val
    max_val = int(max_value) if max_value is not None else existing.get("max")
    if max_val is not None:
        fields["max"] = max_val
    _validate_merged_range("input_text", (min_value, max_value), min_val, max_val)
    _validate_mode("input_text", mode)
    mode_val = mode if mode is not None else existing.get("mode")
    if mode_val is not None:
        fields["mode"] = mode_val
    initial_val = initial if initial is not None else existing.get("initial")
    if initial_val is not None:
        fields["initial"] = initial_val
    return fields


def _update_fields_input_boolean(
    existing: dict[str, Any], initial: Any, **_: Any
) -> dict[str, Any]:
    if initial is not None:
        return {"initial": str(initial).lower() in ["true", "on", "yes", "1"]}
    if "initial" in existing:
        return {"initial": existing["initial"]}
    return {}


def _update_fields_input_datetime(
    existing: dict[str, Any],
    has_date: bool | None,
    has_time: bool | None,
    initial: Any,
    **_: Any,
) -> dict[str, Any]:
    merged_has_date = (
        has_date if has_date is not None else existing.get("has_date", False)
    )
    merged_has_time = (
        has_time if has_time is not None else existing.get("has_time", False)
    )
    _validate_datetime_has_date_or_time(merged_has_date, merged_has_time)
    fields: dict[str, Any] = {"has_date": merged_has_date, "has_time": merged_has_time}
    initial_val = initial if initial is not None else existing.get("initial")
    if initial_val is not None:
        fields["initial"] = initial_val
    return fields


def _update_fields_counter(
    existing: dict[str, Any],
    initial: Any,
    min_value: float | None,
    max_value: float | None,
    step: float | None,
    restore: bool | None,
    **_: Any,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    initial_val = int(initial) if initial is not None else existing.get("initial")
    if initial_val is not None:
        fields["initial"] = initial_val
    minimum_val = int(min_value) if min_value is not None else existing.get("minimum")
    if minimum_val is not None:
        fields["minimum"] = minimum_val
    maximum_val = int(max_value) if max_value is not None else existing.get("maximum")
    if maximum_val is not None:
        fields["maximum"] = maximum_val
    step_val = int(step) if step is not None else existing.get("step")
    if step_val is not None:
        fields["step"] = step_val
    _validate_merged_range(
        "counter", (min_value, max_value, step), minimum_val, maximum_val, step_val
    )
    restore_val = restore if restore is not None else existing.get("restore")
    if restore_val is not None:
        fields["restore"] = restore_val
    return fields


def _update_fields_timer(
    existing: dict[str, Any],
    duration: str | None,
    restore: bool | None,
    **_: Any,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    duration_val = duration if duration is not None else existing.get("duration")
    if duration_val is not None:
        fields["duration"] = duration_val
    restore_val = restore if restore is not None else existing.get("restore")
    if restore_val is not None:
        fields["restore"] = restore_val
    return fields


def _update_fields_schedule(existing: dict[str, Any], **kw: Any) -> dict[str, Any]:
    """schedule/update is full-replace: an unpassed day keeps its stored ranges."""
    passed = _format_schedule_days(*(kw.get(day) for day in _SCHEDULE_DAYS))
    return {day: passed.get(day, existing.get(day, [])) for day in _SCHEDULE_DAYS}


_SIMPLE_UPDATE_FIELD_BUILDERS: dict[str, Callable[..., dict[str, Any]]] = {
    "input_select": _update_fields_input_select,
    "input_number": _update_fields_input_number,
    "input_text": _update_fields_input_text,
    "input_boolean": _update_fields_input_boolean,
    "input_datetime": _update_fields_input_datetime,
    "counter": _update_fields_counter,
    "timer": _update_fields_timer,
    "schedule": _update_fields_schedule,
}


def _build_standard_update_message(
    helper_type: str,
    unique_id: str,
    existing: dict[str, Any],
    name: str | None,
    icon: str | None,
    **kw: Any,
) -> dict[str, Any]:
    """Build the {type}/update WS message for standard input_* types.

    HA's storage-collection update is full-replace (not patch): all vol.Required
    fields must be present even for partial updates, so callers fetch the existing
    config first and merge — passing the new value if provided, else preserving
    the existing value.
    """
    message: dict[str, Any] = {
        "type": f"{helper_type}/update",
        f"{helper_type}_id": unique_id,
        "name": name if name is not None else existing.get("name"),
    }
    if helper_type not in ("person", "tag"):
        # Core rejects an empty icon; a cleared one is left out of the item.
        icon_val = icon if icon is not None else existing.get("icon")
        if icon_val:
            message["icon"] = icon_val
    builder = _SIMPLE_UPDATE_FIELD_BUILDERS.get(helper_type)
    if builder is not None:
        message.update(builder(existing=existing, icon=icon, **kw))
    return message


async def _execute_person_config_update(
    client: Any,
    entity_id: str,
    unique_id: str,
    name: str | None,
    user_id: str | None,
    device_trackers: list[str] | None,
    picture: str | None,
) -> dict[str, Any]:
    """Update a person entity via person/update (full-replace — merges with existing)."""
    list_result = await client.send_websocket_message({"type": "person/list"})
    if not list_result.get("success"):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to fetch person config list: {list_result.get('error', 'Unknown')}",
                context=_simple_helper_error_context("person", entity_id=entity_id),
            )
        )
    person_result = list_result.get("result", {})
    person_list = (
        person_result.get("storage", [])
        if isinstance(person_result, dict)
        else person_result
    )
    current_config = next(
        (p for p in person_list if isinstance(p, dict) and p.get("id") == unique_id),
        None,
    )
    if not current_config:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONFIG_NOT_FOUND,
                f"Person config not found for id: {unique_id}",
                context=_simple_helper_error_context("person", entity_id=entity_id),
            )
        )
    update_msg = _person_update_message(
        unique_id, current_config, name, user_id, device_trackers, picture
    )
    result = await client.send_websocket_message(update_msg)
    if not result.get("success"):
        raise_tool_error(
            create_error_response(
                ws_failure_code(result),
                f"Failed to update person config: {result.get('error', 'Unknown error')}",
                context=_simple_helper_error_context("person", entity_id=entity_id),
            )
        )
    return result.get("result", {})  # type: ignore[no-any-return]


async def _execute_zone_config_update(
    client: Any,
    entity_id: str,
    unique_id: str,
    name: str | None,
    latitude: float | None,
    longitude: float | None,
    radius: float | None,
    passive: bool | None,
) -> dict[str, Any]:
    """Update a zone entity via zone/update."""
    update_msg = _zone_update_message(
        unique_id, name, latitude, longitude, radius, passive
    )
    result = await client.send_websocket_message(update_msg)
    if not result.get("success"):
        raise_tool_error(
            create_error_response(
                ws_failure_code(result),
                f"Failed to update zone config: {result.get('error', 'Unknown error')}",
                context=_simple_helper_error_context("zone", entity_id=entity_id),
            )
        )
    return result.get("result", {})  # type: ignore[no-any-return]


async def _execute_standard_helper_update(
    client: Any,
    helper_type: str,
    entity_id: str,
    unique_id: str,
    name: str | None,
    icon: str | None,
    **kw: Any,
) -> dict[str, Any]:
    """Fetch existing config, merge caller values, and POST update for input_* types."""
    list_result = await client.send_websocket_message({"type": f"{helper_type}/list"})
    if not list_result.get("success"):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to fetch {helper_type} config list: {list_result.get('error', 'Unknown')}",
                context=_simple_helper_error_context(helper_type, entity_id=entity_id),
            )
        )
    existing = next(
        (
            item
            for item in list_result.get("result", [])
            if isinstance(item, dict) and item.get("id") == unique_id
        ),
        None,
    )
    if not existing:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONFIG_NOT_FOUND,
                f"{helper_type} config not found for id: {unique_id}",
                context=_simple_helper_error_context(helper_type, entity_id=entity_id),
            )
        )
    update_msg = _build_standard_update_message(
        helper_type, unique_id, existing, name, icon, **kw
    )
    result = await client.send_websocket_message(update_msg)
    if not result.get("success"):
        raise_tool_error(
            create_error_response(
                ws_failure_code(result),
                f"Failed to update {helper_type} config: {result.get('error', 'Unknown error')}",
                context=_simple_helper_error_context(helper_type, entity_id=entity_id),
            )
        )
    return result.get("result", {})  # type: ignore[no-any-return]


_CONFIG_STORE_TYPES: frozenset[str] = frozenset(
    {
        "person",
        "zone",
        "schedule",
        "input_select",
        "input_number",
        "input_text",
        "input_boolean",
        "input_datetime",
        "counter",
        "timer",
        "input_button",
    }
)


async def _execute_config_store_update(
    client: Any,
    helper_type: str,
    entity_id: str,
    unique_id: str,
    name: str | None,
    icon: str | None,
    **kw: Any,
) -> dict[str, Any]:
    """Dispatch a config-store helper update to the appropriate per-type executor."""
    if helper_type == "person":
        return await _execute_person_config_update(
            client,
            entity_id,
            unique_id,
            name,
            kw.get("user_id"),
            kw.get("device_trackers"),
            kw.get("picture"),
        )
    if helper_type == "zone":
        return await _execute_zone_config_update(
            client,
            entity_id,
            unique_id,
            name,
            kw.get("latitude"),
            kw.get("longitude"),
            kw.get("radius"),
            kw.get("passive"),
        )
    return await _execute_standard_helper_update(
        client, helper_type, entity_id, unique_id, name, icon, **kw
    )


async def _resolve_update_unique_id(
    client: Any,
    helper_type: str,
    entity_id: str,
    helper_id: str | None,
    name: str | None,
) -> str:
    """Look up the unique_id for a helper entity via the entity registry."""
    registry_result = await client.send_websocket_message(
        {
            "type": "config/entity_registry/get",
            "entity_id": entity_id,
        }
    )
    if not registry_result.get("success"):
        suggestions = [
            f"Verify the helper_id={helper_id!r} exists "
            "(use ha_config_list_helpers to list current helpers)",
        ]
        if name:
            suggestions.append(
                f"If you meant to create a new helper named {name!r}, "
                "omit helper_id (or pass action='create')"
            )
        raise_tool_error(
            create_error_response(
                ErrorCode.ENTITY_NOT_FOUND,
                f"Could not find {helper_type} entity: {entity_id}",
                context=_simple_helper_error_context(
                    helper_type, entity_id=entity_id, helper_id=helper_id, name=name
                ),
                suggestions=suggestions,
            )
        )
    registry_entry = registry_result.get("result", {})
    if not isinstance(registry_entry, dict):
        raise_tool_error(
            create_error_response(
                ErrorCode.INTERNAL_ERROR,
                f"Unexpected registry response for {entity_id}",
                context=_simple_helper_error_context(helper_type, entity_id=entity_id),
            )
        )
    unique_id = registry_entry.get("unique_id")
    if not unique_id:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONFIG_NOT_FOUND,
                f"No unique_id found in entity registry for {entity_id}",
                context=_simple_helper_error_context(helper_type, entity_id=entity_id),
            )
        )
    return unique_id  # type: ignore[no-any-return]


async def _apply_update_icon_area_labels(
    client: Any,
    entity_id: str,
    icon: str | None,
    area_id: str | None,
    labels: list[str] | None,
    updated_data: dict[str, Any],
    warnings: list[str],
) -> None:
    """Apply icon/area/labels to the entity registry after a helper update."""
    if icon is None and area_id is None and labels is None:
        return
    registry_update: dict[str, Any] = {
        "type": "config/entity_registry/update",
        "entity_id": entity_id,
    }
    if icon is not None:
        registry_update["icon"] = icon if icon else None
    if area_id is not None:
        registry_update["area_id"] = area_id if area_id else None
    if labels is not None:
        registry_update["labels"] = labels
    async with registry_update_lock("entity", entity_id):
        reg_result = await client.send_websocket_message(registry_update)
    if reg_result.get("success"):
        if icon is not None:
            updated_data["icon"] = icon if icon else None
        if area_id is not None:
            updated_data["area_id"] = area_id if area_id else None
        if labels is not None:
            updated_data["labels"] = labels
    else:
        warnings.append(
            f"Config updated but entity registry update failed: {_ws_error_msg(reg_result)}"
        )


async def _apply_update_registry_and_category(
    client: Any,
    entity_id: str,
    icon: str | None,
    area_id: str | None,
    labels: list[str] | None,
    category: str | None,
    updated_data: dict[str, Any],
    warnings: list[str],
) -> None:
    """Apply icon/area/labels/category to the entity registry after a helper update."""
    await _apply_update_icon_area_labels(
        client, entity_id, icon, area_id, labels, updated_data, warnings
    )

    if category is not None:
        cat_result: dict[str, Any] = {}
        await apply_entity_category(
            client, entity_id, category, "helpers", cat_result, "helper"
        )
        if "category" in cat_result:
            updated_data["category"] = cat_result["category"]
        elif cat_result.get("warnings"):
            warnings.extend(cat_result["warnings"])


async def _execute_fallback_registry_update(
    client: Any,
    helper_type: str,
    entity_id: str,
    name: str | None,
    icon: str | None,
    area_id: str | None,
    labels: list[str] | None,
    category: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    """Update an unknown/future helper type via entity registry update only."""
    fallback_msg: dict[str, Any] = {
        "type": "config/entity_registry/update",
        "entity_id": entity_id,
    }
    if name is not None:
        fallback_msg["name"] = name if name else None
    if icon is not None:
        fallback_msg["icon"] = icon if icon else None
    if area_id is not None:
        fallback_msg["area_id"] = area_id if area_id else None
    if labels is not None:
        fallback_msg["labels"] = labels
    async with registry_update_lock("entity", entity_id):
        result = await client.send_websocket_message(fallback_msg)
    updated_data: dict[str, Any] = {}
    if result.get("success"):
        updated_data = result.get("result", {}).get("entity_entry", {})
    else:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to update helper: {result.get('error', 'Unknown error')}",
                context=_simple_helper_error_context(helper_type, entity_id=entity_id),
            )
        )
    if category is not None:
        cat_result: dict[str, Any] = {}
        await apply_entity_category(
            client, entity_id, category, "helpers", cat_result, "helper"
        )
        if "category" in cat_result:
            updated_data["category"] = cat_result["category"]
        elif cat_result.get("warnings"):
            warnings.extend(cat_result["warnings"])
    return updated_data


async def _execute_update_simple_helper(
    client: Any,
    helper_type: str,
    entity_id: str,
    helper_id: str | None,
    name: str | None,
    icon: str | None,
    area_id: str | None,
    labels: list[str] | None,
    category: str | None,
    wait: bool,
    MandatoryBPS: bool,
    **kw: Any,
) -> dict[str, Any]:
    """Execute the update path for a simple (non-flow) helper type."""
    if not helper_id or not helper_id.strip():
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "helper_id is required for update action",
                context=_simple_helper_error_context(helper_type),
            )
        )

    warnings: list[str] = []
    updated_data: dict[str, Any] = {}

    native = await _update_via_component(
        client,
        helper_type,
        entity_id,
        helper_id,
        name,
        icon,
        area_id,
        labels,
        category,
        **kw,
    )
    if native is not None:
        updated_data, entity_id, warnings = native
        native_response = _helper_response(
            "update",
            helper_type,
            data=updated_data,
            entity_id=entity_id,
            message=f"Successfully updated {helper_type}: {entity_id}",
            warnings=warnings,
        )
        _attach_helper_skill(native_response, MandatoryBPS)
        return native_response

    if helper_type == "tag":
        tag_update_id = await tag_item_id(client, helper_id)
        update_msg = _tag_update_message(tag_update_id, name, kw.get("description"))
        result = await client.send_websocket_message(update_msg)
        if not result.get("success"):
            raise_tool_error(
                create_error_response(
                    ws_failure_code(result),
                    f"Failed to update tag config: {result.get('error', 'Unknown error')}",
                    context=_simple_helper_error_context(
                        helper_type, entity_id=entity_id
                    ),
                )
            )
        tag_data = result.get("result", {})
        tag_entity = await tag_entity_id(client, tag_update_id)
        if tag_entity:
            entity_id = tag_entity
            await _apply_update_registry_and_category(
                client, entity_id, None, area_id, labels, category, tag_data, warnings
            )
        tag_response = _helper_response(
            "update",
            helper_type,
            data=tag_data,
            entity_id=entity_id,
            message=f"Successfully updated {helper_type}: {entity_id}",
            warnings=warnings,
        )
        _attach_helper_skill(tag_response, MandatoryBPS)
        return tag_response

    if helper_type in _CONFIG_STORE_TYPES:
        unique_id = await _resolve_update_unique_id(
            client, helper_type, entity_id, helper_id, name
        )
        updated_data = await _execute_config_store_update(
            client, helper_type, entity_id, unique_id, name, icon, **kw
        )
        await _apply_update_registry_and_category(
            client, entity_id, icon, area_id, labels, category, updated_data, warnings
        )
    else:
        updated_data = await _execute_fallback_registry_update(
            client,
            helper_type,
            entity_id,
            name,
            icon,
            area_id,
            labels,
            category,
            warnings,
        )

    if wait:
        try:
            registered = await wait_for_entity_registered(client, entity_id)
            if not registered:
                warnings.append(f"Update applied but {entity_id} not yet queryable.")
        except Exception as e:  # noqa: BLE001
            warnings.append(f"Update applied but verification failed: {e}")

    update_response = _helper_response(
        "update",
        helper_type,
        data=updated_data,
        entity_id=entity_id,
        message=f"Successfully updated {helper_type}: {entity_id}",
        warnings=warnings,
    )
    _attach_helper_skill(update_response, MandatoryBPS)
    return update_response


def _person_update_message(
    unique_id: str,
    current: dict[str, Any],
    name: str | None,
    user_id: str | None,
    device_trackers: list[str] | None,
    picture: str | None,
    **_: Any,
) -> dict[str, Any]:
    """person/update is full-replace, so unpassed fields keep their current value."""
    update_msg: dict[str, Any] = {
        "type": "person/update",
        "person_id": unique_id,
        "name": name if name is not None else current.get("name"),
        "user_id": user_id if user_id is not None else current.get("user_id"),
        "device_trackers": device_trackers
        if device_trackers is not None
        else current.get("device_trackers", []),
    }
    if picture is not None:
        update_msg["picture"] = picture
    elif current.get("picture"):
        update_msg["picture"] = current["picture"]
    return update_msg


def _zone_update_message(
    unique_id: str,
    name: str | None,
    latitude: float | None,
    longitude: float | None,
    radius: float | None,
    passive: bool | None,
    **_: Any,
) -> dict[str, Any]:
    fields = {
        "name": name,
        "latitude": latitude,
        "longitude": longitude,
        "radius": radius,
        "passive": passive,
    }
    return {
        "type": "zone/update",
        "zone_id": unique_id,
        **{key: value for key, value in fields.items() if value is not None},
    }


def _tag_update_message(
    item_id: str, name: str | None, description: str | None = None, **_: Any
) -> dict[str, Any]:
    update_msg: dict[str, Any] = {"type": "tag/update", "tag_id": item_id}
    if name is not None:
        update_msg["name"] = name
    if description is not None:
        update_msg["description"] = description
    return update_msg


def _build_update_message(
    helper_type: str,
    unique_id: str,
    existing: dict[str, Any],
    name: str | None,
    icon: str | None,
    **kw: Any,
) -> dict[str, Any]:
    """The ``{type}/update`` WS message for any simple helper type."""
    if helper_type == "person":
        return _person_update_message(unique_id, existing, name, **kw)
    if helper_type == "zone":
        return _zone_update_message(unique_id, name, **kw)
    if helper_type == "tag":
        return _tag_update_message(unique_id, name, **kw)
    return _build_standard_update_message(
        helper_type, unique_id, existing, name, icon, **kw
    )


async def _update_via_component(
    client: Any,
    helper_type: str,
    entity_id: str,
    helper_id: str,
    name: str | None,
    icon: str | None,
    area_id: str | None,
    labels: list[str] | None,
    category: str | None,
    **kw: Any,
) -> tuple[dict[str, Any], str, list[str]] | None:
    """Update through Core's collection in-process; ``None`` uses the WS commands.

    A helper the component cannot find also returns ``None``, so the legacy path
    reports it with its usual error.
    """
    if helper_type == "tag":
        target: dict[str, Any] = {"item_id": await tag_item_id(client, helper_id)}
    else:
        target = {"entity_id": entity_id}
    item = await read_helper_item(client, helper_type, **target)
    if item is None:
        return None
    message = _build_update_message(
        helper_type, item["item_id"], item["item"], name, icon, **kw
    )
    registry = {
        key: value
        for key, value in (
            ("icon", icon),
            ("area_id", area_id),
            ("labels", labels),
            ("category", category),
        )
        if value is not None
    }
    result = await write_helper_item(
        client,
        helper_type,
        "update",
        collection_payload(helper_type, message),
        item_id=item["item_id"],
        registry=registry,
        error_context=_simple_helper_error_context(helper_type, entity_id=entity_id),
    )
    return None if result is None else native_result(helper_type, result)
