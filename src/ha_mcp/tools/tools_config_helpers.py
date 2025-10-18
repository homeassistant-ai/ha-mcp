"""
Configuration management tools for Home Assistant helpers.

This module provides tools for listing, creating, updating, and removing
Home Assistant helper entities (input_button, input_boolean, input_select,
input_number, input_text, input_datetime).
"""

import asyncio
import logging
from typing import Annotated, Any, Literal

from pydantic import Field

from .helpers import log_tool_usage
from .util_helpers import parse_string_list_param

logger = logging.getLogger(__name__)


def register_config_helper_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant helper configuration tools."""

    @mcp.tool
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
        - input_button: Virtual buttons
        - input_boolean: Toggle switches
        - input_select: Dropdown lists
        - input_number: Numeric sliders/input boxes
        - input_text: Text input fields
        - input_datetime: Date/time pickers

        EXAMPLES:
        - List all number helpers: ha_config_list_helpers("input_number")
        - List all booleans: ha_config_list_helpers("input_boolean")
        - List all selects: ha_config_list_helpers("input_select")

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

    @mcp.tool
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
                description="Minimum value (input_number) or minimum length (input_text)",
                default=None,
            ),
        ] = None,
        max_value: Annotated[
            float | None,
            Field(
                description="Maximum value (input_number) or maximum length (input_text)",
                default=None,
            ),
        ] = None,
        step: Annotated[
            float | None,
            Field(description="Step/increment value for input_number", default=None),
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
            str | None,
            Field(
                description="Initial value for the helper (input_select, input_text, input_boolean, input_datetime)",
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
    ) -> dict[str, Any]:
        """
        Create or update Home Assistant helper entities for automation and UI control.

        Creates a new helper if helper_id is not provided, or updates an existing helper if helper_id is provided.

        SUPPORTED HELPER TYPES (6/29 total Home Assistant helpers):
        - input_button: Virtual buttons for triggering automations
        - input_boolean: Toggle switches/checkboxes
        - input_datetime: Date and time pickers
        - input_number: Numeric sliders or input boxes
        - input_select: Dropdown selection lists
        - input_text: Text input fields

        EXAMPLES:
        - Create button: ha_config_set_helper("input_button", "My Button", icon="mdi:bell")
        - Create boolean: ha_config_set_helper("input_boolean", "My Switch", icon="mdi:toggle-switch")
        - Create select: ha_config_set_helper("input_select", "My Options", options=["opt1", "opt2", "opt3"])
        - Create number: ha_config_set_helper("input_number", "Temperature", min_value=0, max_value=100, step=0.5, unit_of_measurement="°C")
        - Create datetime: ha_config_set_helper("input_datetime", "My DateTime", has_date=True, has_time=True, initial="2023-12-25 09:00:00")
        - Create date-only: ha_config_set_helper("input_datetime", "My Date", has_date=True, has_time=False, initial="2023-12-25")
        - Update helper: ha_config_set_helper("input_button", "New Name", helper_id="my_button", area_id="living_room", labels=["automation"])

        OTHER HOME ASSISTANT HELPERS (not yet supported):
        Mathematical: bayesian, derivative, filter, integration, min_max, random, statistics, threshold, trend, utility_meter
        Time-based: history_stats, schedule, timer, tod
        Control: counter, generic_hygrostat, generic_thermostat, group, manual, switch_as_x, template
        Environmental: mold_indicator

        **FOR DETAILED HELPER DOCUMENTATION:** Use ha_get_domain_docs() with the specific helper domain.
        For example: ha_get_domain_docs("input_button"), ha_get_domain_docs("input_boolean"), etc.
        This provides comprehensive configuration options, limitations, and advanced features for each helper type.

        **IMPORTANT:** To get help with any specific helper type, use ha_get_domain_docs() with that helper's domain name.
        For instance, to understand all options for input_number helpers, call: ha_get_domain_docs("input_number")
        """
        try:
            # Parse JSON list parameters if provided as strings
            try:
                labels = parse_string_list_param(labels, "labels")
                options = parse_string_list_param(options, "options")
            except ValueError as e:
                return {"success": False, "error": f"Invalid list parameter: {e}"}

            # Determine if this is a create or update based on helper_id
            action = "update" if helper_id else "create"

            if action == "create":
                if not name:
                    return {
                        "success": False,
                        "error": "name is required for create action",
                    }

                # Build create message based on helper type
                message: dict[str, Any] = {"type": f"{helper_type}/create", "name": name}

                if icon:
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
                        message["initial"] = initial.lower() in [
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

                result = await client.send_websocket_message(message)

                if result.get("success"):
                    helper_data = result.get("result", {})
                    entity_id = helper_data.get("entity_id")

                    # Wait for entity to be properly registered before proceeding
                    if entity_id:
                        logger.debug(f"Waiting for {entity_id} to be registered...")
                        # Give the entity a moment to register in the system
                        await asyncio.sleep(0.2)

                        # Verify the entity is accessible via state API
                        max_verification_attempts = 5
                        for attempt in range(max_verification_attempts):
                            try:
                                state_check = await client.get_state(entity_id)
                                if state_check:
                                    logger.debug(
                                        f"Entity {entity_id} verified via state API"
                                    )
                                    break
                            except Exception:
                                pass

                            if attempt < max_verification_attempts - 1:
                                wait_time = 0.1 * (
                                    attempt + 1
                                )  # 0.1s, 0.2s, 0.3s, 0.4s
                                logger.debug(
                                    f"Entity {entity_id} not yet accessible, waiting {wait_time}s..."
                                )
                                await asyncio.sleep(wait_time)

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
                    return {
                        "success": True,
                        "action": "update",
                        "helper_type": helper_type,
                        "entity_id": entity_id,
                        "updated_data": entity_data,
                        "message": f"Successfully updated {helper_type}: {entity_id}",
                    }
                else:
                    return {
                        "success": False,
                        "error": f"Failed to update helper: {result.get('error', 'Unknown error')}",
                        "entity_id": entity_id,
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

    @mcp.tool
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
            ],
            Field(description="Type of helper entity to delete"),
        ],
        helper_id: Annotated[
            str,
            Field(
                description="Helper ID to delete (e.g., 'my_button' or 'input_button.my_button')"
            ),
        ],
    ) -> dict[str, Any]:
        """
        Delete a Home Assistant helper entity.

        SUPPORTED HELPER TYPES:
        - input_button, input_boolean, input_select, input_number, input_text, input_datetime

        EXAMPLES:
        - Delete button: ha_config_remove_helper("input_button", "my_button")
        - Delete number: ha_config_remove_helper("input_number", "input_number.temperature_offset")

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
                    state_check = await client.get_state(entity_id)
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
                    registry_result = await client.send_websocket_message(
                        registry_msg
                    )

                    if registry_result.get("success"):
                        entity_entry = registry_result.get("result", {})
                        unique_id = entity_entry.get("unique_id")
                        if unique_id:
                            logger.info(
                                f"Found unique_id: {unique_id} for {entity_id}"
                            )
                            break

                    # If registry lookup failed but we haven't exhausted retries, wait and try again
                    if attempt < max_retries - 1:
                        wait_time = 0.5 * (2**attempt)  # Exponential backoff
                        logger.debug(
                            f"Registry lookup failed for {entity_id}, waiting {wait_time}s before retry..."
                        )
                        await asyncio.sleep(wait_time)

                except Exception as e:
                    logger.warning(
                        f"Registry lookup attempt {attempt + 1} failed: {e}"
                    )
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

                logger.info(
                    f"Sending fallback WebSocket delete message: {delete_msg}"
                )
                result = await client.send_websocket_message(delete_msg)

                if result.get("success"):
                    return {
                        "success": True,
                        "action": "delete",
                        "helper_type": helper_type,
                        "helper_id": helper_id,
                        "entity_id": entity_id,
                        "method": "fallback_direct_id",
                        "message": f"Successfully deleted {helper_type}: {helper_id} using direct ID (entity: {entity_id})",
                    }

                # Fallback strategy 2: Check if entity was already deleted
                try:
                    final_state_check = await client.get_state(entity_id)
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
                return {
                    "success": True,
                    "action": "delete",
                    "helper_type": helper_type,
                    "helper_id": helper_id,
                    "entity_id": entity_id,
                    "unique_id": unique_id,
                    "method": "standard",
                    "message": f"Successfully deleted {helper_type}: {helper_id} (entity: {entity_id})",
                }
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
