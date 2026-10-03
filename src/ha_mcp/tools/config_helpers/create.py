"""Create path for simple (non-flow) helper types."""

import uuid
from collections.abc import Callable
from typing import Any

from ...errors import ErrorCode, create_error_response
from ...utils.registry_update_lock import registry_update_lock
from ..component_helper_collections import (
    collection_payload,
    native_result,
    tag_entity_id,
    write_helper_item,
)
from ..config_write_helpers import apply_entity_category
from ..helpers import raise_tool_error, ws_failure_code
from ..ws_waiters import wait_for_entity_registered
from .registry import _ws_error_msg
from .schemas import (
    _attach_helper_skill,
    _helper_response,
    _simple_helper_error_context,
)
from .validation import (
    _validate_datetime_has_date_or_time,
    _validate_initial_in_options,
    _validate_mode,
)


def _format_schedule_days(
    monday: list | None,
    tuesday: list | None,
    wednesday: list | None,
    thursday: list | None,
    friday: list | None,
    saturday: list | None,
    sunday: list | None,
) -> dict[str, list[dict[str, Any]]]:
    """Format schedule day data, ensuring time strings include seconds.

    Returns a dict of day_name -> formatted time ranges, only for days
    where data was provided (not None).
    """
    day_params = {
        "monday": monday,
        "tuesday": tuesday,
        "wednesday": wednesday,
        "thursday": thursday,
        "friday": friday,
        "saturday": saturday,
        "sunday": sunday,
    }
    formatted_days: dict[str, list[dict[str, Any]]] = {}
    for day_name, day_schedule in day_params.items():
        if day_schedule is not None:
            formatted_ranges = []
            for time_range in day_schedule:
                formatted_range: dict[str, Any] = {}
                for key in ["from", "to"]:
                    if key in time_range:
                        time_val = time_range[key]
                        if isinstance(time_val, str) and time_val.count(":") == 1:
                            time_val = f"{time_val}:00"
                        formatted_range[key] = time_val
                if "data" in time_range:
                    formatted_range["data"] = time_range["data"]
                formatted_ranges.append(formatted_range)
            formatted_days[day_name] = formatted_ranges
    return formatted_days


# ---------------------------------------------------------------------------
# CREATE INFRASTRUCTURE
# ---------------------------------------------------------------------------


_SCHEDULE_DAYS: tuple[str, ...] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def _create_fields_input_select(
    options: list[str] | None, initial: Any, **_: Any
) -> dict[str, Any]:
    if not options:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "options list is required for input_select",
                context=_simple_helper_error_context("input_select"),
            )
        )
    if not isinstance(options, list) or len(options) == 0:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "options must be a non-empty list for input_select",
                context=_simple_helper_error_context("input_select"),
            )
        )
    fields: dict[str, Any] = {"options": options}
    _validate_initial_in_options(options, initial)
    if initial is not None:
        fields["initial"] = initial
    return fields


def _create_fields_input_number(
    min_value: float | None,
    max_value: float | None,
    step: float | None,
    unit_of_measurement: str | None,
    mode: str | None,
    initial: Any,
    **_: Any,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if min_value is not None:
        fields["min"] = min_value
    if max_value is not None:
        fields["max"] = max_value
    if step is not None:
        fields["step"] = step
    if unit_of_measurement:
        fields["unit_of_measurement"] = unit_of_measurement
    _validate_mode("input_number", mode)
    if mode is not None:
        fields["mode"] = mode
    if initial is not None:
        fields["initial"] = initial
    return fields


def _create_fields_input_text(
    min_value: float | None,
    max_value: float | None,
    mode: str | None,
    initial: Any,
    unit_of_measurement: str | None = None,
    pattern: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        key: value
        for key, value in (
            ("unit_of_measurement", unit_of_measurement),
            ("pattern", pattern),
        )
        if value is not None
    }
    if min_value is not None:
        fields["min"] = int(min_value)
    if max_value is not None:
        fields["max"] = int(max_value)
    _validate_mode("input_text", mode)
    if mode is not None:
        fields["mode"] = mode
    if initial is not None:
        fields["initial"] = initial
    return fields


def _create_fields_input_boolean(initial: Any, **_: Any) -> dict[str, Any]:
    if initial is None:
        return {}
    return {"initial": str(initial).lower() in ["true", "on", "yes", "1"]}


def _create_fields_input_datetime(
    has_date: bool | None,
    has_time: bool | None,
    initial: Any,
    **_: Any,
) -> dict[str, Any]:
    if has_date is None and has_time is None:
        fields: dict[str, Any] = {"has_date": True, "has_time": True}
    elif has_date is None:
        fields = {"has_date": False, "has_time": has_time}
    elif has_time is None:
        fields = {"has_date": has_date, "has_time": False}
    else:
        fields = {"has_date": has_date, "has_time": has_time}
    _validate_datetime_has_date_or_time(fields["has_date"], fields["has_time"])
    if initial is not None:
        fields["initial"] = initial
    return fields


def _create_fields_counter(
    initial: Any,
    min_value: float | None,
    max_value: float | None,
    step: float | None,
    restore: bool | None,
    **_: Any,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if initial is not None:
        fields["initial"] = int(initial) if isinstance(initial, str) else initial
    if min_value is not None:
        fields["minimum"] = int(min_value)
    if max_value is not None:
        fields["maximum"] = int(max_value)
    if step is not None:
        fields["step"] = int(step)
    if restore is not None:
        fields["restore"] = restore
    return fields


def _create_fields_timer(
    duration: str | None, restore: bool | None, **_: Any
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if duration is not None:
        fields["duration"] = duration
    if restore is not None:
        fields["restore"] = restore
    return fields


def _create_fields_schedule(
    monday: list | None,
    tuesday: list | None,
    wednesday: list | None,
    thursday: list | None,
    friday: list | None,
    saturday: list | None,
    sunday: list | None,
    **_: Any,
) -> dict[str, Any]:
    formatted = _format_schedule_days(
        monday, tuesday, wednesday, thursday, friday, saturday, sunday
    )
    if not any(formatted.get(d) for d in _SCHEDULE_DAYS):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "schedule helper requires at least one day-of-week with at least one time range.",
                context=_simple_helper_error_context("schedule"),
                suggestions=[
                    'Pass e.g. monday=[{"from": "08:00", "to": "17:00"}]',
                    'Each day\'s value is a list of {"from": "HH:MM", "to": "HH:MM"} dicts',
                ],
            )
        )
    return formatted


def _create_fields_zone(
    latitude: float | None,
    longitude: float | None,
    radius: float | None,
    passive: bool | None,
    **_: Any,
) -> dict[str, Any]:
    missing = []
    if latitude is None:
        missing.append("latitude")
    if longitude is None:
        missing.append("longitude")
    if missing:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"zone helper requires {' and '.join(missing)}.",
                context=_simple_helper_error_context("zone", missing_fields=missing),
                suggestions=[
                    "Pass latitude (float) and longitude (float)",
                    "Optionally pass radius (meters, default 100) and passive (bool)",
                ],
            )
        )
    fields: dict[str, Any] = {"latitude": latitude, "longitude": longitude}
    if radius is not None:
        fields["radius"] = radius
    if passive is not None:
        fields["passive"] = passive
    return fields


def _create_fields_person(
    user_id: str | None,
    device_trackers: list[str] | None,
    picture: str | None,
    **_: Any,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if user_id:
        fields["user_id"] = user_id
    if device_trackers:
        fields["device_trackers"] = device_trackers
    if picture:
        fields["picture"] = picture
    return fields


def _create_fields_tag(
    tag_id: str | None, description: str | None, **_: Any
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "tag_id": tag_id if tag_id is not None else uuid.uuid4().hex
    }
    if description:
        fields["description"] = description
    return fields


_SIMPLE_CREATE_FIELD_BUILDERS: dict[str, Callable[..., dict[str, Any]]] = {
    "input_select": _create_fields_input_select,
    "input_number": _create_fields_input_number,
    "input_text": _create_fields_input_text,
    "input_boolean": _create_fields_input_boolean,
    "input_datetime": _create_fields_input_datetime,
    "counter": _create_fields_counter,
    "timer": _create_fields_timer,
    "schedule": _create_fields_schedule,
    "zone": _create_fields_zone,
    "person": _create_fields_person,
    "tag": _create_fields_tag,
}


def _build_create_message(
    helper_type: str, name: str, icon: str | None, **kw: Any
) -> dict[str, Any]:
    """Build the WebSocket {type}/create message for a simple helper."""
    message: dict[str, Any] = {"type": f"{helper_type}/create", "name": name}
    if icon and helper_type not in ("person", "tag"):
        message["icon"] = icon
    builder = _SIMPLE_CREATE_FIELD_BUILDERS.get(helper_type)
    if builder is not None:
        message.update(builder(**kw))
    return message


async def _apply_create_entity_registry(
    client: Any,
    entity_id: str,
    icon: str | None,
    area_id: str | None,
    labels: list[str] | None,
    helper_data: dict[str, Any],
    warnings: list[str],
) -> None:
    """Apply area/labels registry update after a simple-helper create; echo into helper_data."""
    if area_id is None and labels is None:
        return
    update_message: dict[str, Any] = {
        "type": "config/entity_registry/update",
        "entity_id": entity_id,
    }
    if area_id is not None:
        update_message["area_id"] = area_id if area_id else None
    if labels is not None:
        update_message["labels"] = labels
    async with registry_update_lock("entity", entity_id):
        update_result = await client.send_websocket_message(update_message)
    if update_result.get("success"):
        if icon is not None:
            helper_data["icon"] = icon if icon else None
        if area_id is not None:
            helper_data["area_id"] = area_id if area_id else None
        if labels is not None:
            helper_data["labels"] = labels
    else:
        warnings.append(
            f"Helper created but entity registry update failed: {_ws_error_msg(update_result)}"
        )


async def _apply_create_category(
    client: Any,
    entity_id: str,
    category: str | None,
    helper_data: dict[str, Any],
    warnings: list[str],
) -> None:
    """Apply category to a newly created helper entity."""
    if not (category and entity_id):
        return
    cat_result: dict[str, Any] = {}
    await apply_entity_category(
        client, entity_id, category, "helpers", cat_result, "helper"
    )
    if "category" in cat_result:
        helper_data["category"] = cat_result["category"]
    elif cat_result.get("warnings"):
        warnings.extend(cat_result["warnings"])


async def _execute_create_simple_helper(
    client: Any,
    helper_type: str,
    name: str | None,
    icon: str | None,
    area_id: str | None,
    labels: list[str] | None,
    category: str | None,
    wait: bool,
    MandatoryBPS: bool,
    **kw: Any,
) -> dict[str, Any]:
    """Execute the create path for a simple (non-flow) helper type."""
    if not name or not name.strip():
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"name is required for create action. Include "
                f'"name" as a top-level argument, e.g. '
                f'{{"helper_type": "{helper_type}", "name": "My Helper"}}.',
                suggestions=[
                    'Add "name": "My Helper" at the top level of the JSON arguments',
                    'Or pass "helper_id": "my_helper" if you intended to update an existing helper',
                ],
                context=_simple_helper_error_context(helper_type),
            )
        )

    message = _build_create_message(helper_type, name, icon, **kw)
    native = await _create_via_component(
        client, helper_type, message, area_id, labels, category
    )
    if native is not None:
        helper_data, entity_id, warnings = native
        create_response = _helper_response(
            "create",
            helper_type,
            data=helper_data,
            entity_id=entity_id,
            message=f"Successfully created {helper_type}: {name}",
            warnings=warnings,
        )
        _attach_helper_skill(create_response, MandatoryBPS)
        return create_response

    result = await client.send_websocket_message(message)
    if not result.get("success"):
        raise_tool_error(
            create_error_response(
                ws_failure_code(result),
                f"Failed to create helper: {result.get('error', 'Unknown error')}",
                context=_simple_helper_error_context(helper_type, name=name),
            )
        )

    helper_data = result.get("result", {})
    entity_id = helper_data.get("entity_id")
    if helper_type == "tag":
        entity_id = await tag_entity_id(client, helper_data.get("id")) or entity_id
    if not entity_id and helper_data.get("id"):
        entity_id = f"{helper_type}.{helper_data['id']}"

    warnings = []
    # Tags live in their own tag registry and never appear in /api/states/<entity_id> —
    # polling there always 404s for the full timeout (~10s per tag), burning CI time.
    if wait and entity_id and helper_type != "tag":
        try:
            registered = await wait_for_entity_registered(client, entity_id)
            if not registered:
                warnings.append(
                    f"Helper created but {entity_id} not yet queryable. It may take a moment to become available."
                )
        except Exception as e:  # noqa: BLE001
            warnings.append(f"Helper created but verification failed: {e}")

    if entity_id:
        await _apply_create_entity_registry(
            client, entity_id, icon, area_id, labels, helper_data, warnings
        )
        await _apply_create_category(client, entity_id, category, helper_data, warnings)

    create_response = _helper_response(
        "create",
        helper_type,
        data=helper_data,
        entity_id=entity_id,
        message=f"Successfully created {helper_type}: {name}",
        warnings=warnings,
    )
    _attach_helper_skill(create_response, MandatoryBPS)
    return create_response


async def _create_via_component(
    client: Any,
    helper_type: str,
    message: dict[str, Any],
    area_id: str | None,
    labels: list[str] | None,
    category: str | None,
) -> tuple[dict[str, Any], str, list[str]] | None:
    """Create through Core's collection in-process; ``None`` uses the WS command.

    The entity exists when Core's create returns, so no ``wait`` polling is needed.
    """
    registry = {
        key: value
        for key, value in (
            ("area_id", area_id),
            ("labels", labels),
            ("category", category),
        )
        if value is not None
    }
    result = await write_helper_item(
        client,
        helper_type,
        "create",
        collection_payload(helper_type, message),
        registry=registry,
        error_context=_simple_helper_error_context(
            helper_type, name=message.get("name")
        ),
    )
    return None if result is None else native_result(helper_type, result)
