"""
Configuration management tools for Home Assistant helpers.

This module provides tools for listing, creating, updating, and removing
Home Assistant helper entities (input_button, input_boolean, input_select,
input_number, input_text, input_datetime, counter, timer, schedule).
"""

import asyncio
import logging
from typing import Annotated, Any, Literal

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, log_tool_usage, raise_tool_error
from .tools_config_entry_flow import (
    FLOW_HELPER_TYPES,
    create_flow_helper,
    update_flow_helper,
)
from .util_helpers import (
    apply_entity_category,
    coerce_bool_param,
    parse_json_param,
    parse_string_list_param,
    wait_for_entity_registered,
    wait_for_entity_removed,
)

# Simple helper types — managed via {type}/create and {type}/update WebSocket APIs
# (not Config Entry Flow). Kept in parallel with FLOW_HELPER_TYPES for routing.
SIMPLE_HELPER_TYPES: frozenset[str] = frozenset({
    "input_button",
    "input_boolean",
    "input_select",
    "input_number",
    "input_text",
    "input_datetime",
    "counter",
    "timer",
    "schedule",
    "zone",
    "person",
    "tag",
})

logger = logging.getLogger(__name__)


async def _get_entities_for_config_entry(
    client: Any, entry_id: str
) -> list[dict[str, Any]]:
    """Return all entity_registry entries linked to the given config_entry_id.

    Uses the config/entity_registry/list WebSocket API and filters client-side
    by config_entry_id. Multi-entity helpers (e.g. utility_meter with tariffs)
    are handled naturally — all entities for the same entry are returned.
    """
    result = await client.send_websocket_message(
        {"type": "config/entity_registry/list"}
    )
    entries = result if isinstance(result, list) else result.get("result", [])
    if not isinstance(entries, list):
        return []
    return [e for e in entries if e.get("config_entry_id") == entry_id]


async def _apply_registry_updates_to_entity(
    client: Any,
    entity_id: str,
    area_id: str | None,
    labels: list[str] | None,
    category: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    """Apply area_id/labels (single WS call) and category (shared helper) to one entity.

    Appends human-readable warning strings to `warnings` on any failure.
    Returns a small dict summarizing what was applied (for result building).
    """
    applied: dict[str, Any] = {"entity_id": entity_id}

    # area_id + labels in one entity_registry/update.
    # Use `is not None` to distinguish "not provided" (no change) from
    # "explicit clear" (empty string / empty list). Mirrors ha_set_entity.
    if area_id is not None or labels is not None:
        update_message: dict[str, Any] = {
            "type": "config/entity_registry/update",
            "entity_id": entity_id,
        }
        if area_id is not None:
            update_message["area_id"] = area_id if area_id else None
        if labels is not None:
            update_message["labels"] = labels
        ws_result = await client.send_websocket_message(update_message)
        if ws_result.get("success"):
            if area_id is not None:
                applied["area_id"] = area_id if area_id else None
            if labels is not None:
                applied["labels"] = labels
        else:
            error_detail = ws_result.get("error", {})
            error_msg = (
                error_detail.get("message", "Unknown error")
                if isinstance(error_detail, dict)
                else str(error_detail)
            )
            warnings.append(
                f"{entity_id}: entity registry update failed: {error_msg}"
            )

    # category via shared helper (consistent with simple helpers / automations / scripts)
    if category:
        cat_ack: dict[str, Any] = {}
        await apply_entity_category(
            client,
            entity_id,
            category,
            "helpers",
            cat_ack,
            "helper",
        )
        if "category" in cat_ack:
            applied["category"] = cat_ack["category"]
        elif "category_warning" in cat_ack:
            warnings.append(f"{entity_id}: {cat_ack['category_warning']}")

    return applied


async def _handle_flow_helper(
    client: Any,
    helper_type: str,
    name: str | None,
    helper_id: str | None,
    config: str | dict | None,
    area_id: str | None,
    labels: str | list[str] | None,
    category: str | None,
    wait: bool | str,
) -> dict[str, Any]:
    """Create or update a flow-based helper and apply registry updates to all entities.

    Routes between create_flow_helper and update_flow_helper based on helper_id,
    then resolves the resulting config_entry_id to its entity(ies) and applies
    area_id / labels / category across the full set.

    For utility_meter with tariffs, this means the same label/area is applied
    to every tariff sensor (and the select entity) uniformly.
    """
    action = "update" if helper_id else "create"

    # Normalize empty string to None, matching ha_config_set_helper's treatment
    # of config in (None, {}, "") as "nothing passed" (L785 simple-type branch).
    # Without this, parse_json_param("") raises a confusing 'Invalid JSON' error.
    if config == "":
        config = None

    # Normalize config into a dict (accepts JSON string or dict).
    if isinstance(config, str):
        parsed = parse_json_param(config)
        if not isinstance(parsed, dict):
            raise_tool_error(create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "config must be a JSON object (dict) for flow-based helpers",
                suggestions=['Example: {"name": "my_helper", "source": "sensor.x"}'],
                context={"helper_type": helper_type},
            ))
        config_dict: dict[str, Any] = parsed
    elif isinstance(config, dict):
        config_dict = dict(config)  # shallow copy — we may mutate
    elif config is None:
        config_dict = {}
    else:
        raise_tool_error(create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"config must be a dict or JSON string, got {type(config).__name__}",
            context={"helper_type": helper_type},
        ))

    # Fold the top-level `name` parameter into config_dict only for create:
    # options (update) flows are strict about extra keys and will reject `name`
    # with 400 "extra keys not allowed @ data['name']" — names on existing flow
    # helpers are not renamed through the options flow.
    if action == "create" and name and "name" not in config_dict:
        config_dict["name"] = name

    # Normalize labels to a list for registry updates below.
    try:
        labels_list = parse_string_list_param(labels, "labels")
    except ValueError as e:
        raise_tool_error(create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"Invalid labels parameter: {e}",
            context={"helper_type": helper_type},
        ))

    # Dispatch to the shared flow machinery.
    if action == "create":
        if not config_dict.get("name"):
            raise_tool_error(create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "name is required for create action",
                suggestions=[
                    "Pass the name argument directly or include 'name' in config",
                ],
                context={"helper_type": helper_type},
            ))
        flow_result = await create_flow_helper(client, helper_type, config_dict)
    else:
        # For updates, helper_id is the config entry_id (flow-based helpers)
        flow_result = await update_flow_helper(
            client, helper_type, config_dict, helper_id  # type: ignore[arg-type]
        )

    entry_id = flow_result.get("entry_id")
    result: dict[str, Any] = {
        "success": True,
        "action": action,
        "helper_type": helper_type,
        "method": "config_flow",
        "entry_id": entry_id,
        "title": flow_result.get("title"),
        "message": flow_result.get("message"),
    }
    if action == "update":
        result["updated"] = True

    # Resolve all entities for this config entry (multi-entity helpers handled naturally).
    # For create with wait=True, poll briefly for at least one entity to appear —
    # otherwise a single fetch is enough (update keeps entities; create without wait
    # is caller-opted into not waiting).
    #
    # Graduated polling: short intervals for the first retries catch local/small
    # instances quickly; steady 500ms matches typical entity_registry/list latency
    # on larger remote setups without missing entities near the deadline.
    warnings: list[str] = []
    wait_bool = coerce_bool_param(wait, "wait", default=True)
    entities: list[dict[str, Any]] = []
    if entry_id:
        if action == "create" and wait_bool:
            deadline = 5.0
            intervals = [0.2, 0.3]  # first two retries faster
            steady_interval = 0.5
            elapsed = 0.0
            attempt = 0
            while elapsed < deadline:
                entities = await _get_entities_for_config_entry(client, entry_id)
                if entities:
                    break
                step = intervals[attempt] if attempt < len(intervals) else steady_interval
                await asyncio.sleep(step)
                elapsed += step
                attempt += 1
        else:
            entities = await _get_entities_for_config_entry(client, entry_id)
    entity_ids = [e["entity_id"] for e in entities if e.get("entity_id")]
    result["entity_ids"] = entity_ids

    # Apply registry updates (area_id / labels / category) to every entity.
    # Use `is not None` so an explicit empty value (area_id="" or labels=[])
    # reaches _apply_registry_updates_to_entity, which forwards the clear
    # semantics (area_id: None / labels: []) to Home Assistant.
    if entity_ids and (
        area_id is not None or labels_list is not None or category is not None
    ):
        applied_per_entity: list[dict[str, Any]] = []
        for eid in entity_ids:
            applied = await _apply_registry_updates_to_entity(
                client, eid, area_id, labels_list, category, warnings
            )
            applied_per_entity.append(applied)
        if area_id is not None:
            result["area_id"] = area_id if area_id else None
        if labels_list is not None:
            result["labels"] = labels_list
        if category:
            result["category"] = category
        result["applied"] = applied_per_entity

    if warnings:
        result["warnings"] = warnings

    return result


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


def register_config_helper_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant helper configuration tools."""

    @mcp.tool(
        tags={"Helper Entities"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "List Helpers",
        },
    )
    @log_tool_usage
    async def ha_config_list_helpers(
        helper_type: Annotated[
            Literal[
                "input_button",
                "input_boolean",
                "input_select",
                "input_number",
                "input_text",
                "input_datetime",
                "counter",
                "timer",
                "schedule",
                "zone",
                "person",
                "tag",
            ],
            Field(description="Type of helper entity to list"),
        ],
    ) -> dict[str, Any]:
        """
        List all Home Assistant helpers of a specific type with their configurations.

        Returns complete configuration for all helpers of the specified type including:
        - ID, name, icon
        - Type-specific settings (min/max for input_number, options for input_select, etc.)
        - Area and label assignments

        SUPPORTED HELPER TYPES:
        - input_button: Virtual buttons for triggering automations
        - input_boolean: Toggle switches/checkboxes
        - input_select: Dropdown selection lists
        - input_number: Numeric sliders/input boxes
        - input_text: Text input fields
        - input_datetime: Date/time pickers
        - counter: Counters with increment/decrement/reset
        - timer: Countdown timers with start/pause/cancel
        - schedule: Weekly schedules with time ranges (on/off per day)
        - zone: Geographical zones for presence detection
        - person: Person entities linked to device trackers
        - tag: NFC/QR tags for automation triggers

        EXAMPLES:
        - List all number helpers: ha_config_list_helpers("input_number")
        - List all counters: ha_config_list_helpers("counter")
        - List all zones: ha_config_list_helpers("zone")
        - List all persons: ha_config_list_helpers("person")
        - List all tags: ha_config_list_helpers("tag")

        **NOTE:** This only returns storage-based helpers (created via UI/API), not YAML-defined helpers.

        For detailed helper documentation, use ha_get_skill_home_assistant_best_practices.
        """
        try:
            # Use the websocket list endpoint for the helper type
            message: dict[str, Any] = {
                "type": f"{helper_type}/list",
            }

            result = await client.send_websocket_message(message)

            if result.get("success"):
                items = result.get("result", [])
                return {
                    "success": True,
                    "helper_type": helper_type,
                    "count": len(items),
                    "helpers": items,
                    "message": f"Found {len(items)} {helper_type} helper(s)",
                }
            else:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to list helpers: {result.get('error', 'Unknown error')}",
                        context={"helper_type": helper_type},
                    )
                )

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error listing helpers: {e}")
            exception_to_structured_error(
                e,
                context={"helper_type": helper_type},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify WebSocket connection is active",
                    "Use ha_search_entities(domain_filter='input_*') as alternative",
                ],
            )

    @mcp.tool(
        tags={"Helper Entities"},
        annotations={"destructiveHint": True, "title": "Create or Update Helper"},
    )
    @log_tool_usage
    async def ha_config_set_helper(
        helper_type: Annotated[
            Literal[
                "counter",
                "derivative",
                "filter",
                "generic_hygrostat",
                "generic_thermostat",
                "group",
                "input_boolean",
                "input_button",
                "input_datetime",
                "input_number",
                "input_select",
                "input_text",
                "integration",
                "min_max",
                "person",
                "random",
                "schedule",
                "statistics",
                "switch_as_x",
                "tag",
                "template",
                "threshold",
                "timer",
                "tod",
                "trend",
                "utility_meter",
                "zone",
            ],
            Field(description="Type of helper entity to create or update"),
        ],
        name: Annotated[
            str | None,
            Field(
                description=(
                    "Display name for the helper. Required on create; optional on "
                    "update (pass helper_id to skip). For flow-based helper types on "
                    "update (template, group, utility_meter, ...), this is typically "
                    "ignored — options flows don't expose renaming. Rename a flow "
                    "helper by deleting and recreating instead."
                ),
                default=None,
            ),
        ] = None,
        helper_id: Annotated[
            str | None,
            Field(
                description="Helper ID for updates (e.g., 'my_button' or 'input_button.my_button'). If not provided, creates a new helper.",
                default=None,
            ),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description="Material Design Icon (e.g., 'mdi:bell', 'mdi:toggle-switch')",
                default=None,
            ),
        ] = None,
        area_id: Annotated[
            str | None,
            Field(description="Area/room ID to assign the helper to", default=None),
        ] = None,
        labels: Annotated[
            str | list[str] | None,
            Field(description="Labels to categorize the helper", default=None),
        ] = None,
        min_value: Annotated[
            float | None,
            Field(
                description="Minimum value (input_number/counter) or minimum length (input_text)",
                default=None,
            ),
        ] = None,
        max_value: Annotated[
            float | None,
            Field(
                description="Maximum value (input_number/counter) or maximum length (input_text)",
                default=None,
            ),
        ] = None,
        step: Annotated[
            float | None,
            Field(
                description="Step/increment value for input_number or counter",
                default=None,
            ),
        ] = None,
        unit_of_measurement: Annotated[
            str | None,
            Field(
                description="Unit of measurement for input_number (e.g., '°C', '%', 'W')",
                default=None,
            ),
        ] = None,
        options: Annotated[
            str | list[str] | None,
            Field(
                description="List of options for input_select (required for input_select)",
                default=None,
            ),
        ] = None,
        initial: Annotated[
            str | int | None,
            Field(
                description="Initial value for the helper (input_select, input_text, input_boolean, input_datetime, counter)",
                default=None,
            ),
        ] = None,
        mode: Annotated[
            str | None,
            Field(
                description="Display mode: 'box'/'slider' for input_number, 'text'/'password' for input_text",
                default=None,
            ),
        ] = None,
        has_date: Annotated[
            bool | None,
            Field(
                description="Include date component for input_datetime", default=None
            ),
        ] = None,
        has_time: Annotated[
            bool | None,
            Field(
                description="Include time component for input_datetime", default=None
            ),
        ] = None,
        restore: Annotated[
            bool | None,
            Field(
                description="Restore state after restart (counter, timer). Defaults to True for counter, False for timer",
                default=None,
            ),
        ] = None,
        duration: Annotated[
            str | None,
            Field(
                description="Default duration for timer in format 'HH:MM:SS' or seconds (e.g., '0:05:00' for 5 minutes)",
                default=None,
            ),
        ] = None,
        monday: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description="Schedule time ranges for Monday. List of {'from': 'HH:MM', 'to': 'HH:MM'} dicts. Optional 'data' dict for additional attributes (e.g. {'from': '07:00', 'to': '22:00', 'data': {'mode': 'comfort'}})",
                default=None,
            ),
        ] = None,
        tuesday: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description="Schedule time ranges for Tuesday. List of {'from': 'HH:MM', 'to': 'HH:MM'} dicts. Optional 'data' dict for additional attributes.",
                default=None,
            ),
        ] = None,
        wednesday: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description="Schedule time ranges for Wednesday. List of {'from': 'HH:MM', 'to': 'HH:MM'} dicts. Optional 'data' dict for additional attributes.",
                default=None,
            ),
        ] = None,
        thursday: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description="Schedule time ranges for Thursday. List of {'from': 'HH:MM', 'to': 'HH:MM'} dicts. Optional 'data' dict for additional attributes.",
                default=None,
            ),
        ] = None,
        friday: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description="Schedule time ranges for Friday. List of {'from': 'HH:MM', 'to': 'HH:MM'} dicts. Optional 'data' dict for additional attributes.",
                default=None,
            ),
        ] = None,
        saturday: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description="Schedule time ranges for Saturday. List of {'from': 'HH:MM', 'to': 'HH:MM'} dicts. Optional 'data' dict for additional attributes.",
                default=None,
            ),
        ] = None,
        sunday: Annotated[
            list[dict[str, Any]] | None,
            Field(
                description="Schedule time ranges for Sunday. List of {'from': 'HH:MM', 'to': 'HH:MM'} dicts. Optional 'data' dict for additional attributes.",
                default=None,
            ),
        ] = None,
        latitude: Annotated[
            float | None,
            Field(
                description="Latitude for zone (required for zone)",
                default=None,
            ),
        ] = None,
        longitude: Annotated[
            float | None,
            Field(
                description="Longitude for zone (required for zone)",
                default=None,
            ),
        ] = None,
        radius: Annotated[
            float | None,
            Field(
                description="Radius in meters for zone (default: 100)",
                default=None,
            ),
        ] = None,
        passive: Annotated[
            bool | None,
            Field(
                description="Passive zone (won't trigger state changes for person entities)",
                default=None,
            ),
        ] = None,
        user_id: Annotated[
            str | None,
            Field(
                description="User ID to link to person entity",
                default=None,
            ),
        ] = None,
        device_trackers: Annotated[
            list[str] | None,
            Field(
                description="List of device_tracker entity IDs for person",
                default=None,
            ),
        ] = None,
        picture: Annotated[
            str | None,
            Field(
                description="Picture URL for person entity",
                default=None,
            ),
        ] = None,
        tag_id: Annotated[
            str | None,
            Field(
                description="Tag ID for tag (auto-generated if not provided)",
                default=None,
            ),
        ] = None,
        description: Annotated[
            str | None,
            Field(
                description="Description for tag",
                default=None,
            ),
        ] = None,
        category: Annotated[
            str | None,
            Field(
                description="Category ID to assign to this helper. Use ha_config_get_category(scope='helpers') to list available categories, or ha_config_set_category() to create one.",
                default=None,
            ),
        ] = None,
        config: Annotated[
            str | dict | None,
            Field(
                description=(
                    "Config dict for flow-based helper types "
                    "(template, group, utility_meter, derivative, min_max, threshold, "
                    "integration, statistics, trend, random, filter, tod, "
                    "generic_thermostat, switch_as_x, generic_hygrostat). "
                    "Accepts JSON string or dict. Ignored for simple helper types. "
                    "Use ha_get_helper_schema(helper_type) to discover required fields."
                ),
                default=None,
            ),
        ] = None,
        wait: Annotated[
            bool | str,
            Field(
                description="Wait for helper entity to be queryable before returning. Default: True. Set to False for bulk operations.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """
        Create or update Home Assistant helper entities (27 types, unified interface).

        Creates new helper if helper_id is omitted, updates existing if helper_id is provided.
        Parameters are validated by Home Assistant - errors return clear messages.

        SIMPLE HELPER TYPES (structured params, WebSocket API):
        - input_boolean, input_button, input_select, input_number, input_text, input_datetime
        - counter, timer, schedule, zone, person, tag

        FLOW-BASED HELPER TYPES (config: dict, Config Entry Flow API):
        - template, group, utility_meter, derivative, min_max, threshold, integration
        - statistics, trend, random, filter, tod, generic_thermostat, switch_as_x, generic_hygrostat

        QUICK EXAMPLES (simple):
        - ha_config_set_helper("input_boolean", "My Switch", icon="mdi:toggle-switch")
        - ha_config_set_helper("counter", "My Counter", initial=0, step=1)
        - ha_config_set_helper("timer", "Laundry", duration="0:45:00")
        - ha_config_set_helper("zone", "Office", latitude=37.77, longitude=-122.41, radius=100)
        - ha_config_set_helper("schedule", "Work", monday=[{"from": "09:00", "to": "17:00"}])
        - ha_config_set_helper("schedule", "Light", monday=[{"from": "07:00", "to": "22:00", "data": {"brightness": "100", "mode": "comfort"}}])

        QUICK EXAMPLES (flow-based, pass config as dict):
        - ha_config_set_helper("min_max", "Avg Temp", config={"entity_ids": ["sensor.t1","sensor.t2"], "type": "mean"})
        - ha_config_set_helper("utility_meter", "Daily kWh", config={"source": "sensor.energy", "cycle": "daily", "tariffs": [], "offset": 0, "net_consumption": False, "delta_values": False, "periodically_resetting": True})
        - ha_config_set_helper("group", "All Lights", config={"group_type": "light", "entities": ["light.a","light.b"], "hide_members": False})
        - ha_config_set_helper("template", "My Sensor", config={"next_step_id": "sensor", "name": "My Sensor", "state": "{{ states('sensor.foo') }}"})

        TEMPLATE SENSORS AND BINARY SENSORS:
        Before reaching for a template, check if a simpler built-in exists:
        - min_max instead of template for combining sensors
        - group instead of template binary sensor for any/all logic
        - counter instead of template with math for counting
        - input_number instead of template for storing values
        - schedule instead of template with weekday checks
        Workflow:
          1. ha_get_helper_schema("template") -> see available sub-types
          2. ha_get_helper_schema("template", menu_option="sensor") -> see form fields
          3. ha_config_set_helper("template", "My Sensor", config={
               "next_step_id": "sensor",
               "name": "My Sensor",
               "state": "{{ states('sensor.foo') }}",
             })

        ROUTING: Flow-based helpers require the config parameter. The name parameter
        is passed into config automatically on create if not already present — but is
        typically ignored on update, because options flows don't expose renaming.
        For flow-based updates, pass helper_id set to the entry_id of the existing entry.
        To rename a flow helper, delete it and recreate with the new name.

        For detailed parameter info, use ha_get_skill_home_assistant_best_practices.
        """
        try:
            # Determine if this is a create or update — set early so the
            # outer exception handler's context dict can reference it even
            # if an exception bubbles out of the flow-helper branch below.
            action = "update" if helper_id else "create"

            # Route flow-based helpers to Config Entry Flow API.
            # Simple helpers continue through the WebSocket {type}/create+update path below.
            if helper_type in FLOW_HELPER_TYPES:
                return await _handle_flow_helper(
                    client=client,
                    helper_type=helper_type,
                    name=name,
                    helper_id=helper_id,
                    config=config,
                    area_id=area_id,
                    labels=labels,
                    category=category,
                    wait=wait,
                )

            # Simple helper types use explicit parameters (name, options, min_value, ...).
            # The `config` parameter only applies to flow-based types; silently ignoring
            # it here would let the caller believe the payload took effect.
            if config not in (None, {}, ""):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"The 'config' parameter is only valid for flow-based helper types. "
                        f"For '{helper_type}', use the explicit parameters (name, options, min_value, etc.).",
                        context={"helper_type": helper_type},
                        suggestions=[
                            f"Pass values for '{helper_type}' via explicit parameters (e.g. options=..., min_value=...)",
                            "For flow-based types (template, group, utility_meter, ...), use 'config' as a dict or JSON string",
                        ],
                    )
                )

            # Parse JSON list parameters if provided as strings
            try:
                labels = parse_string_list_param(labels, "labels")
                options = parse_string_list_param(options, "options")
            except ValueError as e:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid list parameter: {e}",
                    )
                )

            # (action already determined above)

            if action == "create":
                if not name:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            "name is required for create action",
                            context={"helper_type": helper_type},
                        )
                    )

                # Build create message based on helper type
                message: dict[str, Any] = {
                    "type": f"{helper_type}/create",
                    "name": name,
                }

                # Icon supported by most helpers except person and tag
                if icon and helper_type not in ("person", "tag"):
                    message["icon"] = icon

                # Type-specific parameters
                if helper_type == "input_select":
                    if not options:
                        raise_tool_error(
                            create_error_response(
                                ErrorCode.VALIDATION_INVALID_PARAMETER,
                                "options list is required for input_select",
                                context={"helper_type": helper_type},
                            )
                        )
                    if not isinstance(options, list) or len(options) == 0:
                        raise_tool_error(
                            create_error_response(
                                ErrorCode.VALIDATION_INVALID_PARAMETER,
                                "options must be a non-empty list for input_select",
                                context={"helper_type": helper_type},
                            )
                        )
                    message["options"] = options
                    if initial and initial in options:
                        message["initial"] = initial

                elif helper_type == "input_number":
                    # Validate min_value/max_value range
                    if (
                        min_value is not None
                        and max_value is not None
                        and min_value > max_value
                    ):
                        raise_tool_error(
                            create_error_response(
                                ErrorCode.VALIDATION_INVALID_PARAMETER,
                                f"Minimum value ({min_value}) cannot be greater than maximum value ({max_value})",
                                context={
                                    "min_value": min_value,
                                    "max_value": max_value,
                                },
                            )
                        )

                    if min_value is not None:
                        message["min"] = min_value
                    if max_value is not None:
                        message["max"] = max_value
                    if step is not None:
                        message["step"] = step
                    if unit_of_measurement:
                        message["unit_of_measurement"] = unit_of_measurement
                    if mode in ["box", "slider"]:
                        message["mode"] = mode

                elif helper_type == "input_text":
                    if min_value is not None:
                        message["min"] = int(min_value)
                    if max_value is not None:
                        message["max"] = int(max_value)
                    if mode in ["text", "password"]:
                        message["mode"] = mode
                    if initial:
                        message["initial"] = initial

                elif helper_type == "input_boolean":
                    if initial is not None:
                        initial_str = str(initial).lower()
                        message["initial"] = initial_str in [
                            "true",
                            "on",
                            "yes",
                            "1",
                        ]

                elif helper_type == "input_datetime":
                    # At least one of has_date or has_time must be True
                    if has_date is None and has_time is None:
                        # Default to both if not specified
                        message["has_date"] = True
                        message["has_time"] = True
                    elif has_date is None:
                        message["has_date"] = False
                        message["has_time"] = has_time
                    elif has_time is None:
                        message["has_date"] = has_date
                        message["has_time"] = False
                    else:
                        message["has_date"] = has_date
                        message["has_time"] = has_time

                    # Validate that at least one is True
                    if not message["has_date"] and not message["has_time"]:
                        raise_tool_error(
                            create_error_response(
                                ErrorCode.VALIDATION_INVALID_PARAMETER,
                                "At least one of has_date or has_time must be True for input_datetime",
                                context={"helper_type": helper_type},
                            )
                        )

                    if initial:
                        message["initial"] = initial

                elif helper_type == "counter":
                    # Counter parameters: initial, minimum, maximum, step, restore
                    if initial is not None:
                        message["initial"] = (
                            int(initial) if isinstance(initial, str) else initial
                        )
                    if min_value is not None:
                        message["minimum"] = int(min_value)
                    if max_value is not None:
                        message["maximum"] = int(max_value)
                    if step is not None:
                        message["step"] = int(step)
                    if restore is not None:
                        message["restore"] = restore

                elif helper_type == "timer":
                    # Timer parameters: duration, restore
                    if duration:
                        message["duration"] = duration
                    if restore is not None:
                        message["restore"] = restore

                elif helper_type == "schedule":
                    # Schedule parameters: monday-sunday with time ranges
                    # Each day is a list of {"from": "HH:MM:SS", "to": "HH:MM:SS"}
                    # with optional "data" dict for additional attributes
                    message.update(
                        _format_schedule_days(
                            monday,
                            tuesday,
                            wednesday,
                            thursday,
                            friday,
                            saturday,
                            sunday,
                        )
                    )

                elif helper_type == "zone":
                    # Zone parameters - HA validates required fields (latitude, longitude)
                    if latitude is not None:
                        message["latitude"] = latitude
                    if longitude is not None:
                        message["longitude"] = longitude
                    if radius is not None:
                        message["radius"] = radius
                    if passive is not None:
                        message["passive"] = passive

                elif helper_type == "person":
                    # Person parameters: user_id, device_trackers, picture
                    if user_id:
                        message["user_id"] = user_id
                    if device_trackers:
                        message["device_trackers"] = device_trackers
                    if picture:
                        message["picture"] = picture

                elif helper_type == "tag":
                    # Tag parameters: tag_id, description
                    # Note: name goes into entity registry, not tag storage
                    if tag_id:
                        message["tag_id"] = tag_id
                    if description:
                        message["description"] = description

                result = await client.send_websocket_message(message)

                if result.get("success"):
                    helper_data = result.get("result", {})
                    entity_id = helper_data.get("entity_id")
                    # Some helper types don't return entity_id — derive from result id
                    if not entity_id and helper_data.get("id"):
                        entity_id = f"{helper_type}.{helper_data['id']}"

                    # Wait for entity to be properly registered before proceeding
                    wait_bool = coerce_bool_param(wait, "wait", default=True)
                    if wait_bool and entity_id:
                        try:
                            registered = await wait_for_entity_registered(
                                client, entity_id
                            )
                            if not registered:
                                helper_data["warning"] = (
                                    f"Helper created but {entity_id} not yet queryable. It may take a moment to become available."
                                )
                        except Exception as e:
                            helper_data["warning"] = (
                                f"Helper created but verification failed: {e}"
                            )

                    # Update entity registry if area_id or labels specified
                    if (area_id is not None or labels is not None) and entity_id:
                        update_message: dict[str, Any] = {
                            "type": "config/entity_registry/update",
                            "entity_id": entity_id,
                        }
                        if area_id is not None:
                            update_message["area_id"] = area_id if area_id else None
                        if labels is not None:
                            update_message["labels"] = labels

                        update_result = await client.send_websocket_message(
                            update_message
                        )
                        if update_result.get("success"):
                            if area_id is not None:
                                helper_data["area_id"] = area_id if area_id else None
                            if labels is not None:
                                helper_data["labels"] = labels
                        else:
                            error_detail = update_result.get("error", {})
                            error_msg = (
                                error_detail.get("message", "Unknown error")
                                if isinstance(error_detail, dict)
                                else str(error_detail)
                            )
                            helper_data["warning"] = (
                                f"Helper created but entity registry update failed: {error_msg}"
                            )

                    # Apply category via shared helper (consistent with automations/scripts)
                    if category and entity_id:
                        await apply_entity_category(
                            client,
                            entity_id,
                            category,
                            "helpers",
                            helper_data,
                            "helper",
                        )

                    return {
                        "success": True,
                        "action": "create",
                        "helper_type": helper_type,
                        "helper_data": helper_data,
                        "entity_id": entity_id,
                        "message": f"Successfully created {helper_type}: {name}",
                    }
                else:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.SERVICE_CALL_FAILED,
                            f"Failed to create helper: {result.get('error', 'Unknown error')}",
                            context={"helper_type": helper_type, "name": name},
                        )
                    )

            elif action == "update":
                if not helper_id:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            "helper_id is required for update action",
                            context={"helper_type": helper_type},
                        )
                    )

                entity_id = (
                    helper_id
                    if helper_id.startswith(helper_type)
                    else f"{helper_type}.{helper_id}"
                )

                # Helper types that persist config in dedicated storage APIs
                # (not just the entity registry). Each type uses its own
                # {type}/update WebSocket command. Tags use their own
                # registry and don't have entity registry entries.
                config_store_types = {
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

                updated_data: dict[str, Any] = {}

                if helper_type == "tag":
                    # Tags use their own registry — no entity registry entries.
                    # The helper_id IS the tag_id (strip "tag." prefix if present).
                    tag_update_id = (
                        helper_id.removeprefix("tag.")
                        if helper_id.startswith("tag.")
                        else helper_id
                    )
                    update_msg: dict[str, Any] = {
                        "type": "tag/update",
                        "tag_id": tag_update_id,
                    }
                    if name is not None:
                        update_msg["name"] = name
                    if description is not None:
                        update_msg["description"] = description

                    result = await client.send_websocket_message(update_msg)
                    if not result.get("success"):
                        raise_tool_error(
                            create_error_response(
                                ErrorCode.SERVICE_CALL_FAILED,
                                f"Failed to update tag config: {result.get('error', 'Unknown error')}",
                                context={
                                    "helper_type": helper_type,
                                    "entity_id": entity_id,
                                },
                            )
                        )
                    updated_data = result.get("result", {})

                    # Tags don't have entity registry entries, so return directly
                    # without wait_for_entity_registered (they're not entities).
                    return {
                        "success": True,
                        "action": "update",
                        "helper_type": helper_type,
                        "entity_id": entity_id,
                        "updated_data": updated_data,
                        "message": f"Successfully updated {helper_type}: {entity_id}",
                    }

                elif helper_type in config_store_types:
                    # Person and zone: look up unique_id from entity registry
                    registry_msg: dict[str, Any] = {
                        "type": "config/entity_registry/get",
                        "entity_id": entity_id,
                    }
                    registry_result = await client.send_websocket_message(registry_msg)
                    if not registry_result.get("success"):
                        raise_tool_error(
                            create_error_response(
                                ErrorCode.ENTITY_NOT_FOUND,
                                f"Could not find {helper_type} entity: {entity_id}",
                                context={
                                    "helper_type": helper_type,
                                    "entity_id": entity_id,
                                },
                            )
                        )
                    registry_entry = registry_result.get("result", {})
                    if not isinstance(registry_entry, dict):
                        raise_tool_error(
                            create_error_response(
                                ErrorCode.INTERNAL_ERROR,
                                f"Unexpected registry response for {entity_id}",
                                context={
                                    "helper_type": helper_type,
                                    "entity_id": entity_id,
                                },
                            )
                        )
                    unique_id = registry_entry.get("unique_id")
                    if not unique_id:
                        raise_tool_error(
                            create_error_response(
                                ErrorCode.CONFIG_NOT_FOUND,
                                f"No unique_id found in entity registry for {entity_id}",
                                context={
                                    "helper_type": helper_type,
                                    "entity_id": entity_id,
                                },
                            )
                        )

                    if helper_type == "person":
                        # Person config API is full-replace (not patch):
                        # fetch current config, merge with new values, then send.
                        list_result = await client.send_websocket_message(
                            {"type": "person/list"}
                        )
                        if not list_result.get("success"):
                            raise_tool_error(
                                create_error_response(
                                    ErrorCode.SERVICE_CALL_FAILED,
                                    f"Failed to fetch person config list: {list_result.get('error', 'Unknown')}",
                                    context={
                                        "helper_type": helper_type,
                                        "entity_id": entity_id,
                                    },
                                )
                            )

                        # person/list returns {"storage": [...], "config": [...]}
                        # "storage" contains UI-managed (editable) persons
                        person_result = list_result.get("result", {})
                        person_list = (
                            person_result.get("storage", [])
                            if isinstance(person_result, dict)
                            else person_result
                        )

                        current_config = next(
                            (
                                p
                                for p in person_list
                                if isinstance(p, dict) and p.get("id") == unique_id
                            ),
                            None,
                        )

                        if not current_config:
                            raise_tool_error(
                                create_error_response(
                                    ErrorCode.CONFIG_NOT_FOUND,
                                    f"Person config not found for id: {unique_id}",
                                    context={
                                        "helper_type": helper_type,
                                        "entity_id": entity_id,
                                    },
                                )
                            )

                        # Merge: use new values if provided, else keep current
                        update_msg = {
                            "type": "person/update",
                            "person_id": unique_id,
                            "name": name
                            if name is not None
                            else current_config.get("name"),
                            "user_id": user_id
                            if user_id is not None
                            else current_config.get("user_id"),
                            "device_trackers": device_trackers
                            if device_trackers is not None
                            else current_config.get("device_trackers", []),
                        }
                        if picture is not None:
                            update_msg["picture"] = picture
                        elif current_config.get("picture"):
                            update_msg["picture"] = current_config["picture"]

                        result = await client.send_websocket_message(update_msg)
                        if not result.get("success"):
                            raise_tool_error(
                                create_error_response(
                                    ErrorCode.SERVICE_CALL_FAILED,
                                    f"Failed to update person config: {result.get('error', 'Unknown error')}",
                                    context={
                                        "helper_type": helper_type,
                                        "entity_id": entity_id,
                                    },
                                )
                            )
                        updated_data = result.get("result", {})

                    elif helper_type == "zone":
                        update_msg = {
                            "type": "zone/update",
                            "zone_id": unique_id,
                        }
                        if name is not None:
                            update_msg["name"] = name
                        if latitude is not None:
                            update_msg["latitude"] = latitude
                        if longitude is not None:
                            update_msg["longitude"] = longitude
                        if radius is not None:
                            update_msg["radius"] = radius
                        if passive is not None:
                            update_msg["passive"] = passive

                        result = await client.send_websocket_message(update_msg)
                        if not result.get("success"):
                            raise_tool_error(
                                create_error_response(
                                    ErrorCode.SERVICE_CALL_FAILED,
                                    f"Failed to update zone config: {result.get('error', 'Unknown error')}",
                                    context={
                                        "helper_type": helper_type,
                                        "entity_id": entity_id,
                                    },
                                )
                            )
                        updated_data = result.get("result", {})

                    elif helper_type == "schedule":
                        update_msg = {
                            "type": "schedule/update",
                            "schedule_id": unique_id,
                        }
                        if name is not None:
                            update_msg["name"] = name
                        if icon is not None:
                            update_msg["icon"] = icon

                        update_msg.update(
                            _format_schedule_days(
                                monday,
                                tuesday,
                                wednesday,
                                thursday,
                                friday,
                                saturday,
                                sunday,
                            )
                        )

                        result = await client.send_websocket_message(update_msg)
                        if not result.get("success"):
                            raise_tool_error(
                                create_error_response(
                                    ErrorCode.SERVICE_CALL_FAILED,
                                    f"Failed to update schedule config: {result.get('error', 'Unknown error')}",
                                    context={
                                        "helper_type": helper_type,
                                        "entity_id": entity_id,
                                    },
                                )
                            )
                        updated_data = result.get("result", {})

                    else:
                        # Standard input helpers: use {type}/update API
                        # to persist config changes (not just entity registry).
                        # HA's update schemas require all vol.Required fields
                        # even for partial updates, so fetch current config
                        # and backfill any fields the caller didn't provide.
                        list_result = await client.send_websocket_message(
                            {"type": f"{helper_type}/list"}
                        )
                        if not list_result.get("success"):
                            raise_tool_error(
                                create_error_response(
                                    ErrorCode.SERVICE_CALL_FAILED,
                                    f"Failed to fetch {helper_type} config list: {list_result.get('error', 'Unknown')}",
                                    context={
                                        "helper_type": helper_type,
                                        "entity_id": entity_id,
                                    },
                                )
                            )
                        existing = next(
                            (
                                item
                                for item in list_result.get("result", [])
                                if isinstance(item, dict)
                                and item.get("id") == unique_id
                            ),
                            None,
                        )
                        if not existing:
                            raise_tool_error(
                                create_error_response(
                                    ErrorCode.CONFIG_NOT_FOUND,
                                    f"{helper_type} config not found for id: {unique_id}",
                                    context={
                                        "helper_type": helper_type,
                                        "entity_id": entity_id,
                                    },
                                )
                            )

                        update_msg = {
                            "type": f"{helper_type}/update",
                            f"{helper_type}_id": unique_id,
                            "name": name
                            if name is not None
                            else existing.get("name"),
                        }
                        if icon is not None:
                            update_msg["icon"] = icon

                        if helper_type == "input_select":
                            update_msg["options"] = (
                                options
                                if options is not None
                                else existing.get("options", [])
                            )
                            if initial is not None:
                                update_msg["initial"] = initial

                        elif helper_type == "input_number":
                            update_msg["min"] = (
                                min_value
                                if min_value is not None
                                else existing.get("min", 0)
                            )
                            update_msg["max"] = (
                                max_value
                                if max_value is not None
                                else existing.get("max", 100)
                            )
                            if step is not None:
                                update_msg["step"] = step
                            if unit_of_measurement is not None:
                                update_msg["unit_of_measurement"] = unit_of_measurement
                            if mode in ["box", "slider"]:
                                update_msg["mode"] = mode

                        elif helper_type == "input_text":
                            if min_value is not None:
                                update_msg["min"] = int(min_value)
                            if max_value is not None:
                                update_msg["max"] = int(max_value)
                            if mode in ["text", "password"]:
                                update_msg["mode"] = mode
                            if initial is not None:
                                update_msg["initial"] = initial

                        elif helper_type == "input_boolean":
                            if initial is not None:
                                initial_str = str(initial).lower()
                                update_msg["initial"] = initial_str in [
                                    "true",
                                    "on",
                                    "yes",
                                    "1",
                                ]

                        elif helper_type == "input_datetime":
                            if has_date is not None:
                                update_msg["has_date"] = has_date
                            if has_time is not None:
                                update_msg["has_time"] = has_time
                            if initial is not None:
                                update_msg["initial"] = initial

                        elif helper_type == "counter":
                            if initial is not None:
                                update_msg["initial"] = int(initial)
                            if min_value is not None:
                                update_msg["minimum"] = int(min_value)
                            if max_value is not None:
                                update_msg["maximum"] = int(max_value)
                            if step is not None:
                                update_msg["step"] = int(step)
                            if restore is not None:
                                update_msg["restore"] = restore

                        elif helper_type == "timer":
                            if duration is not None:
                                update_msg["duration"] = duration
                            if restore is not None:
                                update_msg["restore"] = restore

                        # input_button has no type-specific params beyond name/icon

                        result = await client.send_websocket_message(update_msg)
                        if not result.get("success"):
                            raise_tool_error(
                                create_error_response(
                                    ErrorCode.SERVICE_CALL_FAILED,
                                    f"Failed to update {helper_type} config: {result.get('error', 'Unknown error')}",
                                    context={
                                        "helper_type": helper_type,
                                        "entity_id": entity_id,
                                    },
                                )
                            )
                        updated_data = result.get("result", {})

                    # Also update entity registry for icon, area, and labels
                    if icon is not None or area_id is not None or labels is not None:
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
                        reg_result = await client.send_websocket_message(
                            registry_update
                        )
                        if not reg_result.get("success"):
                            error_detail = reg_result.get("error", {})
                            error_msg = (
                                error_detail.get("message", "Unknown error")
                                if isinstance(error_detail, dict)
                                else str(error_detail)
                            )
                            logger.warning(
                                f"Entity registry update failed for {entity_id}: {error_msg}"
                            )
                            updated_data["warning"] = (
                                f"Config updated but entity registry update failed: {error_msg}"
                            )

                    # Apply category via shared helper
                    if category:
                        await apply_entity_category(
                            client,
                            entity_id,
                            category,
                            "helpers",
                            updated_data,
                            "helper",
                        )

                else:
                    # Fallback for unknown/future helper types: entity registry update only
                    update_msg = {
                        "type": "config/entity_registry/update",
                        "entity_id": entity_id,
                    }

                    if name is not None:
                        update_msg["name"] = name if name else None
                    if icon is not None:
                        update_msg["icon"] = icon if icon else None
                    if area_id is not None:
                        update_msg["area_id"] = area_id if area_id else None
                    if labels is not None:
                        update_msg["labels"] = labels

                    result = await client.send_websocket_message(update_msg)

                    if result.get("success"):
                        updated_data = result.get("result", {}).get("entity_entry", {})
                    else:
                        raise_tool_error(
                            create_error_response(
                                ErrorCode.SERVICE_CALL_FAILED,
                                f"Failed to update helper: {result.get('error', 'Unknown error')}",
                                context={
                                    "helper_type": helper_type,
                                    "entity_id": entity_id,
                                },
                            )
                        )

                    # Apply category via shared helper
                    if category:
                        await apply_entity_category(
                            client,
                            entity_id,
                            category,
                            "helpers",
                            updated_data,
                            "helper",
                        )

                # Wait for entity to reflect the update
                wait_bool = coerce_bool_param(wait, "wait", default=True)
                response: dict[str, Any] = {
                    "success": True,
                    "action": "update",
                    "helper_type": helper_type,
                    "entity_id": entity_id,
                    "updated_data": updated_data,
                    "message": f"Successfully updated {helper_type}: {entity_id}",
                }
                if wait_bool:
                    try:
                        registered = await wait_for_entity_registered(client, entity_id)
                        if not registered:
                            response["warning"] = (
                                f"Update applied but {entity_id} not yet queryable."
                            )
                    except Exception as e:
                        response["warning"] = (
                            f"Update applied but verification failed: {e}"
                        )
                return response

            # This should never be reached since action is either "create" or "update"
            raise_tool_error(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    f"Unexpected action: {action}",
                )
            )

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"action": action, "helper_type": helper_type},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify helper_id exists for update operations",
                    "Ensure required parameters are provided for the helper type",
                ],
            )

    @mcp.tool(
        tags={"Helper Entities"},
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "title": "Remove Helper",
        },
    )
    @log_tool_usage
    async def ha_config_remove_helper(
        helper_type: Annotated[
            Literal[
                "input_button",
                "input_boolean",
                "input_select",
                "input_number",
                "input_text",
                "input_datetime",
                "counter",
                "timer",
                "schedule",
                "zone",
                "person",
                "tag",
            ],
            Field(description="Type of helper entity to delete"),
        ],
        helper_id: Annotated[
            str,
            Field(
                description="Helper ID to delete (e.g., 'my_button' or 'input_button.my_button')"
            ),
        ],
        wait: Annotated[
            bool | str,
            Field(
                description="Wait for helper entity to be fully removed before returning. Default: True.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """
        Delete a Home Assistant helper entity.

        SUPPORTED HELPER TYPES:
        - input_button, input_boolean, input_select, input_number, input_text, input_datetime
        - counter, timer, schedule, zone, person, tag

        EXAMPLES:
        - Delete button: ha_config_remove_helper("input_button", "my_button")
        - Delete counter: ha_config_remove_helper("counter", "my_counter")
        - Delete timer: ha_config_remove_helper("timer", "my_timer")
        - Delete schedule: ha_config_remove_helper("schedule", "work_hours")

        **WARNING:** Deleting a helper that is used by automations or scripts may cause those automations/scripts to fail.
        Use ha_search_entities() to verify the helper exists before attempting to delete it.
        """
        try:
            # Convert helper_id to full entity_id if needed
            entity_id = (
                helper_id
                if helper_id.startswith(helper_type)
                else f"{helper_type}.{helper_id}"
            )

            # Try to get unique_id with retry logic to handle race conditions
            unique_id = None
            registry_result = None
            max_retries = 3

            for attempt in range(max_retries):
                logger.info(
                    f"Getting entity registry for: {entity_id} (attempt {attempt + 1}/{max_retries})"
                )

                # Check if entity exists via state API first (faster check)
                try:
                    state_check = await client.get_entity_state(entity_id)
                    if not state_check:
                        # Entity doesn't exist in state, wait a bit for registration
                        if attempt < max_retries - 1:
                            wait_time = 0.5 * (
                                2**attempt
                            )  # Exponential backoff: 0.5s, 1s, 2s
                            logger.debug(
                                f"Entity {entity_id} not found in state, waiting {wait_time}s before retry..."
                            )
                            await asyncio.sleep(wait_time)
                            continue
                except Exception as e:
                    logger.debug(f"State check failed for {entity_id}: {e}")

                # Try registry lookup
                registry_msg: dict[str, Any] = {
                    "type": "config/entity_registry/get",
                    "entity_id": entity_id,
                }

                try:
                    registry_result = await client.send_websocket_message(registry_msg)

                    if registry_result.get("success"):
                        entity_entry = registry_result.get("result", {})
                        unique_id = entity_entry.get("unique_id")
                        if unique_id:
                            logger.info(f"Found unique_id: {unique_id} for {entity_id}")
                            break

                    # If registry lookup failed but we haven't exhausted retries, wait and try again
                    if attempt < max_retries - 1:
                        wait_time = 0.5 * (2**attempt)  # Exponential backoff
                        logger.debug(
                            f"Registry lookup failed for {entity_id}, waiting {wait_time}s before retry..."
                        )
                        await asyncio.sleep(wait_time)

                except Exception as e:
                    logger.warning(f"Registry lookup attempt {attempt + 1} failed: {e}")
                    if attempt < max_retries - 1:
                        wait_time = 0.5 * (2**attempt)
                        await asyncio.sleep(wait_time)

            # Fallback strategy 1: Try deletion with helper_id directly if unique_id not found
            if not unique_id:
                logger.info(
                    f"Could not find unique_id for {entity_id}, trying direct deletion with helper_id"
                )

                # Try deleting using helper_id directly (fallback approach)
                delete_msg: dict[str, Any] = {
                    "type": f"{helper_type}/delete",
                    f"{helper_type}_id": helper_id,
                }

                logger.info(f"Sending fallback WebSocket delete message: {delete_msg}")
                result = await client.send_websocket_message(delete_msg)

                if result.get("success"):
                    # Wait for entity to be removed
                    wait_bool = coerce_bool_param(wait, "wait", default=True)
                    response: dict[str, Any] = {
                        "success": True,
                        "action": "delete",
                        "helper_type": helper_type,
                        "helper_id": helper_id,
                        "entity_id": entity_id,
                        "method": "fallback_direct_id",
                        "message": f"Successfully deleted {helper_type}: {helper_id} using direct ID (entity: {entity_id})",
                    }
                    if wait_bool:
                        try:
                            removed = await wait_for_entity_removed(client, entity_id)
                            if not removed:
                                response["warning"] = (
                                    f"Deletion confirmed but {entity_id} may still appear briefly."
                                )
                        except Exception as e:
                            response["warning"] = (
                                f"Deletion confirmed but removal verification failed: {e}"
                            )
                    return response

                # Fallback strategy 2: Check if entity was already deleted
                try:
                    final_state_check = await client.get_entity_state(entity_id)
                    if not final_state_check:
                        logger.info(
                            f"Entity {entity_id} no longer exists, considering deletion successful"
                        )
                        return {
                            "success": True,
                            "action": "delete",
                            "helper_type": helper_type,
                            "helper_id": helper_id,
                            "entity_id": entity_id,
                            "method": "already_deleted",
                            "message": f"Helper {helper_id} was already deleted or never properly registered",
                        }
                except Exception:
                    pass

                # Final fallback failed
                raise_tool_error(
                    create_error_response(
                        ErrorCode.ENTITY_NOT_FOUND,
                        f"Helper not found in entity registry after {max_retries} attempts: {registry_result.get('error', 'Unknown error') if registry_result else 'No registry response'}",
                        suggestions=[
                            "Helper may not be properly registered or was already deleted. Use ha_search_entities() to verify.",
                        ],
                        context={"helper_id": helper_id, "entity_id": entity_id},
                    )
                )

            # Delete helper using unique_id (correct API from docs)
            delete_message: dict[str, Any] = {
                "type": f"{helper_type}/delete",
                f"{helper_type}_id": unique_id,
            }

            logger.info(f"Sending WebSocket delete message: {delete_message}")
            result = await client.send_websocket_message(delete_message)
            logger.info(f"WebSocket delete response: {result}")

            if result.get("success"):
                # Wait for entity to be removed
                wait_bool = coerce_bool_param(wait, "wait", default=True)
                response = {
                    "success": True,
                    "action": "delete",
                    "helper_type": helper_type,
                    "helper_id": helper_id,
                    "entity_id": entity_id,
                    "unique_id": unique_id,
                    "method": "standard",
                    "message": f"Successfully deleted {helper_type}: {helper_id} (entity: {entity_id})",
                }
                if wait_bool:
                    try:
                        removed = await wait_for_entity_removed(client, entity_id)
                        if not removed:
                            response["warning"] = (
                                f"Deletion confirmed but {entity_id} may still appear briefly."
                            )
                    except Exception as e:
                        response["warning"] = (
                            f"Deletion confirmed but removal verification failed: {e}"
                        )
                return response
            else:
                error_msg = result.get("error", "Unknown error")
                # Handle specific HA error messages
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))

                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to delete helper: {error_msg}",
                        suggestions=[
                            "Make sure the helper exists and is not being used by automations or scripts",
                        ],
                        context={
                            "helper_id": helper_id,
                            "entity_id": entity_id,
                            "unique_id": unique_id,
                        },
                    )
                )

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"helper_type": helper_type, "helper_id": helper_id},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify helper_id exists using ha_search_entities()",
                    "Ensure helper is not being used by automations or scripts",
                ],
            )
