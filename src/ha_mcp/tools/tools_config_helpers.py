"""
Configuration management tools for Home Assistant helpers.

This module provides tools for listing, creating, updating, and removing
Home Assistant helper entities (input_button, input_boolean, input_select,
input_number, input_text, input_datetime, counter, timer, schedule).
"""

import asyncio
import logging
from typing import Annotated, Any, Literal

from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .helpers import log_tool_usage
from .util_helpers import coerce_bool_param, parse_string_list_param, wait_for_entity_registered, wait_for_entity_removed

logger = logging.getLogger(__name__)


def register_config_helper_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant helper configuration tools."""

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["helper"],
            "title": "List Helpers",
        }
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

        For detailed helper documentation, use: ha_get_domain_docs("input_number"), etc.
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
                return {
                    "success": False,
                    "error": f"Failed to list helpers: {result.get('error', 'Unknown error')}",
                    "helper_type": helper_type,
                }

        except Exception as e:
            logger.error(f"Error listing helpers: {e}")
            return {
                "success": False,
                "error": f"Failed to list {helper_type} helpers: {str(e)}",
                "helper_type": helper_type,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify WebSocket connection is active",
                    "Use ha_search_entities(domain_filter='input_*') as alternative",
                ],
            }

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "tags": ["helper"],
            "title": "Create or Update Helper",
        }
    )
    @log_tool_usage
    async def ha_config_set_helper(
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
            Field(description="Type of helper entity to create or update"),
        ],
        name: Annotated[str, Field(description="Display name for the helper")],
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
        wait: Annotated[
            bool | str,
            Field(
                description="Wait for helper entity to be queryable before returning. Default: True. Set to False for bulk operations.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """
        Create or update Home Assistant helper entities.

        Creates new helper if helper_id is omitted, updates existing if helper_id is provided.
        Parameters are validated by Home Assistant - errors return clear messages.

        QUICK EXAMPLES:
        - ha_config_set_helper("input_boolean", "My Switch", icon="mdi:toggle-switch")
        - ha_config_set_helper("counter", "My Counter", initial=0, step=1)
        - ha_config_set_helper("timer", "Laundry", duration="0:45:00")
        - ha_config_set_helper("zone", "Office", latitude=37.77, longitude=-122.41, radius=100)
        - ha_config_set_helper("schedule", "Work", monday=[{"from": "09:00", "to": "17:00"}])
        - ha_config_set_helper("schedule", "Light", monday=[{"from": "07:00", "to": "22:00", "data": {"brightness": "100", "mode": "comfort"}}])

        PREFER BUILT-IN HELPERS OVER TEMPLATE SENSORS:
        Before creating a template sensor, check if a built-in helper/integration exists:
        - Use `min_max` integration (type: mean/min/max/sum) instead of template for combining sensors
        - Use `group` instead of template binary sensor for any/all logic
        - Use `counter` instead of template with math for counting
        - Use `input_number` instead of template for storing values
        - Use `schedule` instead of template with weekday checks

        For detailed parameter info: ha_get_domain_docs("counter"), ha_get_domain_docs("zone"), etc.
        """
        try:
            # Parse JSON list parameters if provided as strings
            try:
                labels = parse_string_list_param(labels, "labels")
                options = parse_string_list_param(options, "options")
            except ValueError as e:
                return create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid list parameter: {e}",
                )

            # Determine if this is a create or update based on helper_id
            action = "update" if helper_id else "create"

            if action == "create":
                if not name:
                    return {
                        "success": False,
                        "error": "name is required for create action",
                    }

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
                        return {
                            "success": False,
                            "error": "options list is required for input_select",
                        }
                    if not isinstance(options, list) or len(options) == 0:
                        return {
                            "success": False,
                            "error": "options must be a non-empty list for input_select",
                        }
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
                        return {
                            "success": False,
                            "error": f"Minimum value ({min_value}) cannot be greater than maximum value ({max_value})",
                            "min_value": min_value,
                            "max_value": max_value,
                        }

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
                        return {
                            "success": False,
                            "error": "At least one of has_date or has_time must be True for input_datetime",
                        }

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
                    day_params = {
                        "monday": monday,
                        "tuesday": tuesday,
                        "wednesday": wednesday,
                        "thursday": thursday,
                        "friday": friday,
                        "saturday": saturday,
                        "sunday": sunday,
                    }
                    for day_name, day_schedule in day_params.items():
                        if day_schedule is not None:
                            # Ensure time format has seconds
                            formatted_ranges = []
                            for time_range in day_schedule:
                                formatted_range = {}
                                for key in ["from", "to"]:
                                    if key in time_range:
                                        time_val = time_range[key]
                                        # Add seconds if not present
                                        if time_val.count(":") == 1:
                                            time_val = f"{time_val}:00"
                                        formatted_range[key] = time_val
                                # Pass through the optional 'data' dict
                                # for additional attributes (e.g. mode, brightness)
                                if "data" in time_range:
                                    formatted_range["data"] = time_range["data"]
                                formatted_ranges.append(formatted_range)
                            message[day_name] = formatted_ranges

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

                    # Wait for entity to be properly registered before proceeding
                    wait_bool = coerce_bool_param(wait, "wait", default=True)
                    if wait_bool and entity_id:
                        try:
                            registered = await wait_for_entity_registered(client, entity_id)
                            if not registered:
                                helper_data["warning"] = f"Helper created but {entity_id} not yet queryable. It may take a moment to become available."
                        except Exception as e:
                            helper_data["warning"] = f"Helper created but verification failed: {e}"

                    # Update entity registry if area_id or labels specified
                    if (area_id or labels) and entity_id:
                        update_message: dict[str, Any] = {
                            "type": "config/entity_registry/update",
                            "entity_id": entity_id,
                        }
                        if area_id:
                            update_message["area_id"] = area_id
                        if labels:
                            update_message["labels"] = labels

                        update_result = await client.send_websocket_message(
                            update_message
                        )
                        if update_result.get("success"):
                            helper_data["area_id"] = area_id
                            helper_data["labels"] = labels

                    return {
                        "success": True,
                        "action": "create",
                        "helper_type": helper_type,
                        "helper_data": helper_data,
                        "entity_id": entity_id,
                        "message": f"Successfully created {helper_type}: {name}",
                    }
                else:
                    return {
                        "success": False,
                        "error": f"Failed to create helper: {result.get('error', 'Unknown error')}",
                        "helper_type": helper_type,
                        "name": name,
                    }

            elif action == "update":
                if not helper_id:
                    return {
                        "success": False,
                        "error": "helper_id is required for update action",
                    }

                # For updates, we primarily use entity registry update
                entity_id = (
                    helper_id
                    if helper_id.startswith(helper_type)
                    else f"{helper_type}.{helper_id}"
                )

                update_msg: dict[str, Any] = {
                    "type": "config/entity_registry/update",
                    "entity_id": entity_id,
                }

                if name:
                    update_msg["name"] = name
                if icon:
                    update_msg["icon"] = icon
                if area_id:
                    update_msg["area_id"] = area_id
                if labels:
                    update_msg["labels"] = labels

                result = await client.send_websocket_message(update_msg)

                if result.get("success"):
                    entity_data = result.get("result", {}).get("entity_entry", {})

                    # Wait for entity to reflect the update
                    wait_bool = coerce_bool_param(wait, "wait", default=True)
                    response: dict[str, Any] = {
                        "success": True,
                        "action": "update",
                        "helper_type": helper_type,
                        "entity_id": entity_id,
                        "updated_data": entity_data,
                        "message": f"Successfully updated {helper_type}: {entity_id}",
                    }
                    if wait_bool:
                        try:
                            registered = await wait_for_entity_registered(client, entity_id)
                            if not registered:
                                response["warning"] = f"Update applied but {entity_id} not yet queryable."
                        except Exception as e:
                            response["warning"] = f"Update applied but verification failed: {e}"
                    return response
                else:
                    return {
                        "success": False,
                        "error": f"Failed to update helper: {result.get('error', 'Unknown error')}",
                        "entity_id": entity_id,
                    }

            # This should never be reached since action is either "create" or "update"
            return {
                "success": False,
                "error": f"Unexpected action: {action}",
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"Helper management failed: {str(e)}",
                "action": action,
                "helper_type": helper_type,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify helper_id exists for update operations",
                    "Ensure required parameters are provided for the helper type",
                ],
            }

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "tags": ["helper"],
            "title": "Remove Helper",
        }
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
                                response["warning"] = f"Deletion confirmed but {entity_id} may still appear briefly."
                        except Exception as e:
                            response["warning"] = f"Deletion confirmed but removal verification failed: {e}"
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
                return {
                    "success": False,
                    "error": f"Helper not found in entity registry after {max_retries} attempts: {registry_result.get('error', 'Unknown error') if registry_result else 'No registry response'}",
                    "helper_id": helper_id,
                    "entity_id": entity_id,
                    "suggestion": "Helper may not be properly registered or was already deleted. Use ha_search_entities() to verify.",
                }

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
                            response["warning"] = f"Deletion confirmed but {entity_id} may still appear briefly."
                    except Exception as e:
                        response["warning"] = f"Deletion confirmed but removal verification failed: {e}"
                return response
            else:
                error_msg = result.get("error", "Unknown error")
                # Handle specific HA error messages
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))

                return {
                    "success": False,
                    "error": f"Failed to delete helper: {error_msg}",
                    "helper_id": helper_id,
                    "entity_id": entity_id,
                    "unique_id": unique_id,
                    "suggestion": "Make sure the helper exists and is not being used by automations or scripts",
                }

        except Exception as e:
            return {
                "success": False,
                "error": f"Helper deletion failed: {str(e)}",
                "helper_type": helper_type,
                "helper_id": helper_id,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify helper_id exists using ha_search_entities()",
                    "Ensure helper is not being used by automations or scripts",
                ],
            }
