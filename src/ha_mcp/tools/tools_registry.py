"""
Device Registry management tools for Home Assistant.

This module provides tools for managing devices (list, get details, update, remove).

Important: Device renaming does NOT cascade to entities - they are independent registries.
"""

import logging
from typing import Annotated, Any, Literal

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..client.rest_client import HomeAssistantAPIError, HomeAssistantConnectionError
from ..errors import ErrorCode, create_error_response
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
)
from .util_helpers import (
    build_pagination_metadata,
    coerce_int_param,
    parse_string_list_param,
)

logger = logging.getLogger(__name__)


def register_registry_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register device registry management tools."""

    async def _update_device_internal(
        device_id: str,
        name: str | None = None,
        area_id: str | None = None,
        disabled_by: str | None = None,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Internal implementation of device update."""
        try:
            # Build update message
            message: dict[str, Any] = {
                "type": "config/device_registry/update",
                "device_id": device_id,
            }

            updates_made = []

            if name is not None:
                message["name_by_user"] = name if name else None
                updates_made.append(f"name='{name}'" if name else "name cleared")

            if area_id is not None:
                message["area_id"] = area_id if area_id else None
                updates_made.append(
                    f"area_id='{area_id}'" if area_id else "area cleared"
                )

            if disabled_by is not None:
                message["disabled_by"] = disabled_by if disabled_by else None
                updates_made.append(
                    f"disabled_by='{disabled_by}'" if disabled_by else "enabled"
                )

            if labels is not None:
                message["labels"] = labels
                updates_made.append(f"labels={labels}")

            if not updates_made:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "No updates specified",
                        suggestions=[
                            "Provide at least one of: name, area_id, disabled_by, or labels",
                        ],
                        context={"device_id": device_id},
                    )
                )

            logger.info(f"Updating device {device_id}: {', '.join(updates_made)}")
            result = await client.send_websocket_message(message)

            if result.get("success"):
                device_entry = result.get("result", {})
                return {
                    "success": True,
                    "device_id": device_id,
                    "updates": updates_made,
                    "device_entry": {
                        "name": device_entry.get("name_by_user")
                        or device_entry.get("name"),
                        "name_by_user": device_entry.get("name_by_user"),
                        "area_id": device_entry.get("area_id"),
                        "disabled_by": device_entry.get("disabled_by"),
                        "labels": device_entry.get("labels", []),
                    },
                    "message": f"Device updated: {', '.join(updates_made)}",
                    "note": "Remember: Device rename does NOT cascade to entities. Use ha_set_entity(new_entity_id=...) to rename entities.",
                }
            else:
                error = result.get("error", {})
                error_msg = (
                    error.get("message", str(error))
                    if isinstance(error, dict)
                    else str(error)
                )
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to update device: {error_msg}",
                        suggestions=[
                            "Verify the device_id exists using ha_get_device()",
                            "Check that area_id exists if specified",
                        ],
                        context={"device_id": device_id},
                    )
                )

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error updating device: {e}")
            exception_to_structured_error(
                e,
                context={"device_id": device_id},
            )

    @mcp.tool(
        tags={"Device Registry", "Zigbee", "Z-Wave"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get Device (incl. Zigbee/ZHA/Z2M and Z-Wave)",
        },
    )
    @log_tool_usage
    async def ha_get_device(
        device_id: Annotated[
            str | None,
            Field(
                description="Device ID to retrieve details for. If omitted, lists devices.",
                default=None,
            ),
        ] = None,
        entity_id: Annotated[
            str | None,
            Field(
                description="Entity ID to find the associated device for (e.g., 'light.living_room')",
                default=None,
            ),
        ] = None,
        integration: Annotated[
            str | None,
            Field(
                description="Filter devices by integration: 'zha', 'zigbee2mqtt', 'zwave_js', 'mqtt', 'hue', etc.",
                default=None,
            ),
        ] = None,
        area_id: Annotated[
            str | None,
            Field(
                description="Filter devices by area ID (e.g., 'living_room')",
                default=None,
            ),
        ] = None,
        manufacturer: Annotated[
            str | None,
            Field(
                description="Filter devices by manufacturer name (e.g., 'Philips')",
                default=None,
            ),
        ] = None,
        limit: Annotated[
            int | str,
            Field(
                default=50,
                description="Max devices to return per page in list mode (default: 50)",
            ),
        ] = 50,
        offset: Annotated[
            int | str,
            Field(
                default=0,
                description="Number of devices to skip for pagination (default: 0)",
            ),
        ] = 0,
        detail_level: Annotated[
            Literal["summary", "full"],
            Field(
                default="summary",
                description=(
                    "'summary': basic device info and protocol identifiers (default for list mode). "
                    "'full': include entities and all integration details. "
                    "Single device lookups always return full detail."
                ),
            ),
        ] = "summary",
    ) -> dict[str, Any]:
        """Get device information with pagination, including Zigbee (ZHA/Z2M) and Z-Wave JS devices.

        Without device_id/entity_id: Lists devices with optional filters and pagination.
        With device_id or entity_id: Returns full detail for that specific device.

        **List devices (paginated):**
        - First page: ha_get_device()
        - Next page: ha_get_device(offset=50)
        - By area: ha_get_device(area_id="living_room")
        - By integration: ha_get_device(integration="zigbee2mqtt")
        - Full details in list: ha_get_device(detail_level="full", limit=10)

        **Single device lookup (always full detail):**
        - By device_id: ha_get_device(device_id="abc123")
        - By entity_id: ha_get_device(entity_id="light.living_room")

        **Zigbee:** integration="zha" or "zigbee2mqtt". Returns ieee_address, radio metrics.
        **Z-Wave:** integration="zwave_js". Returns node_id, node_status.
        """
        try:
            limit_int = coerce_int_param(
                limit, "limit", default=50, min_value=1, max_value=200
            )
            offset_int = coerce_int_param(offset, "offset", default=0, min_value=0)
            effective_detail = detail_level

            # Get device registry
            list_message: dict[str, Any] = {"type": "config/device_registry/list"}
            list_result = await client.send_websocket_message(list_message)

            if not list_result.get("success"):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to access device registry: {list_result.get('error', 'Unknown error')}",
                    )
                )

            all_devices = list_result.get("result", [])

            # Get entity registry
            entity_message: dict[str, Any] = {"type": "config/entity_registry/list"}
            entity_result = await client.send_websocket_message(entity_message)
            all_entities = (
                entity_result.get("result", []) if entity_result.get("success") else []
            )

            # Build entity -> device_id map (always needed for entity_id param lookup)
            # Build device -> entities map only when needed (single device lookup or full detail)
            need_entity_details = device_id or entity_id or effective_detail == "full"
            entity_to_device: dict[str, str] = {}
            device_to_entities: dict[str, list[dict[str, Any]]] = {}
            for e in all_entities:
                eid = e.get("entity_id")
                did = e.get("device_id")
                if eid and did:
                    entity_to_device[eid] = did
                    if need_entity_details:
                        if did not in device_to_entities:
                            device_to_entities[did] = []
                        device_to_entities[did].append(
                            {
                                "entity_id": eid,
                                "name": e.get("name") or e.get("original_name"),
                                "platform": e.get("platform"),
                            }
                        )

            # If entity_id provided, find the device_id
            if entity_id and not device_id:
                device_id = entity_to_device.get(entity_id)
                if not device_id:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.ENTITY_NOT_FOUND,
                            f"Entity '{entity_id}' not found or has no associated device",
                            suggestions=[
                                "Use ha_search_entities() to find valid entity IDs",
                            ],
                            context={"entity_id": entity_id},
                        )
                    )

            # Helper function to extract integration info from a device
            def get_device_info(device: dict[str, Any]) -> dict[str, Any]:
                identifiers = device.get("identifiers", [])
                connections = device.get("connections", [])

                # Determine integration type and extract IEEE/node addresses
                integration_sources = []
                ieee_address = None
                zwave_node_id = None
                friendly_name = device.get("name_by_user") or device.get("name")
                is_z2m = False

                for identifier in identifiers:
                    if isinstance(identifier, (list, tuple)) and len(identifier) >= 2:
                        domain = identifier[0]
                        value = str(identifier[1])
                        if domain not in integration_sources:
                            integration_sources.append(domain)

                        # ZHA: identifier is ["zha", "IEEE_ADDRESS"]
                        if domain == "zha":
                            ieee_address = value

                        # Z2M: identifier is ["mqtt", "zigbee2mqtt_0xIEEE"]
                        if domain == "mqtt" and "zigbee2mqtt" in value.lower():
                            is_z2m = True
                            # Extract IEEE from "zigbee2mqtt_0x..." or "zigbee2mqtt_bridge_0x..."
                            if "_0x" in value:
                                ieee_address = "0x" + value.split("_0x")[-1]

                        # Z-Wave JS: identifier is ["zwave_js", "{home_id}-{node_id}"]
                        if domain == "zwave_js" and "-" in value:
                            zwave_node_id = value.split("-")[1]

                # Also check connections for IEEE
                for connection in connections:
                    if isinstance(connection, (list, tuple)) and len(connection) >= 2:
                        if connection[0] == "ieee" and not ieee_address:
                            ieee_address = connection[1]

                # Determine primary integration type
                if "zha" in integration_sources:
                    integration_type = "zha"
                elif is_z2m:
                    integration_type = "zigbee2mqtt"
                elif "zwave_js" in integration_sources:
                    integration_type = "zwave_js"
                elif "mqtt" in integration_sources:
                    integration_type = "mqtt"
                elif integration_sources:
                    integration_type = integration_sources[0]
                else:
                    integration_type = "unknown"

                device_info: dict[str, Any] = {
                    "device_id": device.get("id"),
                    "name": friendly_name,
                    "manufacturer": device.get("manufacturer"),
                    "model": device.get("model"),
                    "sw_version": device.get("sw_version"),
                    "area_id": device.get("area_id"),
                    "integration_type": integration_type,
                    "integration_sources": integration_sources,
                    "via_device_id": device.get("via_device_id"),
                }

                # Add Zigbee-specific info
                if ieee_address:
                    device_info["ieee_address"] = ieee_address

                if integration_type == "zigbee2mqtt":
                    device_info["friendly_name"] = friendly_name
                    device_info["mqtt_topic_hint"] = f"zigbee2mqtt/{friendly_name}/..."

                if integration_type == "zha" and ieee_address:
                    device_info["zha_trigger_hint"] = (
                        f"Use ieee '{ieee_address}' for zha_event triggers"
                    )

                # Add Z-Wave specific info
                if integration_type == "zwave_js" and zwave_node_id:
                    device_info["node_id"] = zwave_node_id

                return device_info

            # Single device lookup mode
            if device_id:
                device = next(
                    (d for d in all_devices if d.get("id") == device_id), None
                )
                if not device:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.ENTITY_NOT_FOUND,
                            f"Device not found: {device_id}",
                            suggestions=[
                                "Use ha_get_device() to find valid device IDs",
                            ],
                            context={"device_id": device_id},
                        )
                    )

                device_info = get_device_info(device)
                device_info["entities"] = device_to_entities.get(device_id, [])

                # Add extra fields for single lookup
                device_info["name_by_user"] = device.get("name_by_user")
                device_info["default_name"] = device.get("name")
                device_info["hw_version"] = device.get("hw_version")
                device_info["serial_number"] = device.get("serial_number")
                device_info["disabled_by"] = device.get("disabled_by")
                device_info["labels"] = device.get("labels", [])
                device_info["config_entries"] = device.get("config_entries", [])
                device_info["connections"] = device.get("connections", [])
                device_info["identifiers"] = device.get("identifiers", [])

                # Enrich ZHA devices with radio metrics (LQI/RSSI)
                if device_info.get("integration_type") == "zha" and device_info.get(
                    "ieee_address"
                ):
                    try:
                        zha_result = await client.send_websocket_message(
                            {"type": "zha/devices"}
                        )
                        if zha_result.get("success"):
                            # Build ieee→metrics map for O(1) lookup
                            zha_by_ieee = {
                                d.get("ieee"): d
                                for d in zha_result.get("result", [])
                                if d.get("ieee")
                            }
                            target_ieee = device_info["ieee_address"]
                            zha_dev = zha_by_ieee.get(target_ieee)
                            if zha_dev:
                                device_info["radio_metrics"] = {
                                    "lqi": zha_dev.get("lqi"),
                                    "rssi": zha_dev.get("rssi"),
                                }
                    except (
                        HomeAssistantConnectionError,
                        HomeAssistantAPIError,
                        TimeoutError,
                        OSError,
                    ) as e:
                        logger.warning(
                            "Could not fetch ZHA radio metrics for device %s: %s",
                            device_info.get("device_id"),
                            e,
                        )

                # Enrich Z-Wave JS devices with node status
                if device_info.get(
                    "integration_type"
                ) == "zwave_js" and device_info.get("node_id"):
                    try:
                        zwave_result = await client.send_websocket_message(
                            {"type": "zwave_js/node_status", "device_id": device_id}
                        )
                        if zwave_result.get("success"):
                            node_data = zwave_result.get("result", {})
                            device_info["node_status"] = {
                                "node_id": node_data.get("node_id"),
                                "status": node_data.get("status"),
                                "is_routing": node_data.get("is_routing"),
                                "is_secure": node_data.get("is_secure"),
                                "highest_security_class": node_data.get(
                                    "highest_security_class"
                                ),
                                "zwave_plus_version": node_data.get(
                                    "zwave_plus_version"
                                ),
                                "is_controller_node": node_data.get(
                                    "is_controller_node"
                                ),
                            }
                    except (
                        HomeAssistantConnectionError,
                        HomeAssistantAPIError,
                        TimeoutError,
                        OSError,
                    ) as e:
                        logger.warning(
                            "Could not fetch Z-Wave node status for device %s: %s",
                            device_info.get("device_id"),
                            e,
                        )

                entities = device_info.get("entities", [])
                return {
                    "success": True,
                    "device": device_info,
                    "entities": entities,  # Also at top level for backward compatibility
                    "entity_count": len(entities),
                    "queried_by": "entity_id" if entity_id else "device_id",
                    "queried_entity_id": entity_id,
                }

            # List mode - filter devices by any combination of filters
            matched_devices = []
            integration_lower = integration.lower() if integration else None
            manufacturer_lower = manufacturer.lower() if manufacturer else None

            for device in all_devices:
                # Apply area filter
                if area_id and device.get("area_id") != area_id:
                    continue

                # Apply manufacturer filter
                if manufacturer_lower:
                    device_manufacturer = (device.get("manufacturer") or "").lower()
                    if manufacturer_lower not in device_manufacturer:
                        continue

                device_info = get_device_info(device)

                # Apply integration filter if specified
                if integration_lower:
                    # Match integration — named types get exact match,
                    # others match against integration_sources list
                    named_types = ["zigbee2mqtt", "zha", "zwave_js"]
                    if integration_lower in named_types:
                        if device_info["integration_type"] != integration_lower:
                            continue
                    elif integration_lower not in device_info.get(
                        "integration_sources", []
                    ):
                        continue

                # In summary mode, omit entity lists to reduce response size
                if effective_detail == "full":
                    device_info["entities"] = device_to_entities.get(
                        device.get("id"), []
                    )
                matched_devices.append(device_info)

            # Apply pagination
            total_matched = len(matched_devices)
            paginated_devices = matched_devices[offset_int : offset_int + limit_int]

            # Build result
            result: dict[str, Any] = {
                "success": True,
                **build_pagination_metadata(
                    total_matched, offset_int, limit_int, len(paginated_devices)
                ),
                "total_devices": len(all_devices),
                "devices": paginated_devices,
                "detail_level": effective_detail,
            }

            # Add filter info
            filters_applied = []
            if integration:
                result["integration_filter"] = integration
                filters_applied.append(f"integration={integration}")
            if area_id:
                result["area_filter"] = area_id
                filters_applied.append(f"area_id={area_id}")
            if manufacturer:
                result["manufacturer_filter"] = manufacturer
                filters_applied.append(f"manufacturer={manufacturer}")

            if filters_applied:
                result["filters"] = filters_applied

            # Find bridge device for Z2M
            if integration_lower == "zigbee2mqtt":
                bridge_info = None
                for d in matched_devices:
                    if (
                        d.get("via_device_id") is None
                        and "bridge" in (d.get("name") or "").lower()
                    ):
                        bridge_info = {
                            "device_id": d.get("device_id"),
                            "name": d.get("name"),
                            "ieee_address": d.get("ieee_address"),
                        }
                        break
                if bridge_info:
                    result["bridge"] = bridge_info
                result["usage_hint"] = (
                    "Use 'friendly_name' for MQTT topics: zigbee2mqtt/{friendly_name}/action"
                )
            elif integration_lower == "zha":
                result["usage_hint"] = (
                    "Use 'ieee_address' for zha_event triggers in automations"
                )
            elif integration_lower == "zwave_js":
                result["usage_hint"] = (
                    "Use node_id for Z-Wave device identification. "
                    "Single device lookup includes node status (security, routing)."
                )

            return result

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error getting device: {e}")
            exception_to_structured_error(e)

    @mcp.tool(
        tags={"Device Registry"},
        annotations={"destructiveHint": True, "title": "Update Device"},
    )
    @log_tool_usage
    async def ha_update_device(
        device_id: Annotated[
            str,
            Field(description="Device ID to update"),
        ],
        name: Annotated[
            str | None,
            Field(
                description="New display name for the device (sets name_by_user)",
                default=None,
            ),
        ] = None,
        area_id: Annotated[
            str | None,
            Field(
                description="Area/room ID to assign the device to. Use empty string '' to unassign.",
                default=None,
            ),
        ] = None,
        disabled_by: Annotated[
            str | None,
            Field(
                description="Set to 'user' to disable, or None/empty string to enable",
                default=None,
            ),
        ] = None,
        labels: Annotated[
            str | list[str] | None,
            Field(
                description="Labels to assign to the device (replaces existing labels)",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Update device properties such as name, area, disabled state, or labels.

        IMPORTANT: Renaming a device does NOT rename its entities!
        Device and entity names are independent. To rename entities, use ha_set_entity(new_entity_id=...).

        Common workflow for full rename:
        1. ha_update_device(device_id="abc", name="Living Room Sensor")  # Rename device
        2. ha_set_entity("sensor.old", new_entity_id="sensor.living_room")  # Rename entities separately

        PARAMETERS:
        - name: Sets the user-defined display name (name_by_user)
        - area_id: Assigns device to an area/room. Use '' to remove from area.
        - disabled_by: Set to 'user' to disable, or empty to enable
        - labels: List of labels (replaces existing labels)

        EXAMPLES:
        - Rename device: ha_update_device("abc123", name="Living Room Hub")
        - Move to area: ha_update_device("abc123", area_id="living_room")
        - Disable device: ha_update_device("abc123", disabled_by="user")
        - Enable device: ha_update_device("abc123", disabled_by="")
        - Add labels: ha_update_device("abc123", labels=["important", "sensor"])
        """
        # Parse labels if provided as string
        parsed_labels = None
        if labels is not None:
            try:
                parsed_labels = parse_string_list_param(labels, "labels")
            except ValueError as e:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid labels parameter: {e}",
                    )
                )

        # Delegate to internal implementation
        return await _update_device_internal(
            device_id=device_id,
            name=name,
            area_id=area_id,
            disabled_by=disabled_by,
            labels=parsed_labels,
        )

    @mcp.tool(
        tags={"Device Registry"},
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "title": "Remove Device",
        },
    )
    @log_tool_usage
    async def ha_remove_device(
        device_id: Annotated[
            str,
            Field(description="Device ID to remove from the registry"),
        ],
    ) -> dict[str, Any]:
        """
        Remove an orphaned device from the Home Assistant device registry.

        WARNING: This removes the device entry from the registry.
        - Use only for orphaned devices that are no longer connected
        - Active devices will typically be re-added by their integration
        - Associated entities may also be removed

        This uses the config entry removal which is the safe way to remove devices.
        If the device has multiple config entries, they must all be removed.

        EXAMPLES:
        - Remove orphaned device: ha_remove_device("abc123def456")

        NOTE: For most use cases, consider disabling the device instead:
        ha_update_device(device_id="abc123", disabled_by="user")
        """
        try:
            # First, get device details to find config entries
            list_message: dict[str, Any] = {"type": "config/device_registry/list"}
            list_result = await client.send_websocket_message(list_message)

            if not list_result.get("success"):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to access device registry: {list_result.get('error', 'Unknown error')}",
                    )
                )

            devices = list_result.get("result", [])
            device = next((d for d in devices if d.get("id") == device_id), None)

            if not device:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.ENTITY_NOT_FOUND,
                        f"Device not found: {device_id}",
                        suggestions=[
                            "Use ha_get_device() to find valid device IDs",
                        ],
                        context={"device_id": device_id},
                    )
                )

            config_entries = device.get("config_entries", [])

            if not config_entries:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        "Device has no config entries - cannot be removed via this method",
                        suggestions=[
                            "This device may be managed by an integration directly. Try disabling it instead.",
                        ],
                        context={
                            "device_id": device_id,
                            "device_name": device.get("name_by_user")
                            or device.get("name"),
                        },
                    )
                )

            # Remove device from each config entry
            removal_results = []
            for config_entry_id in config_entries:
                remove_message: dict[str, Any] = {
                    "type": "config/device_registry/remove_config_entry",
                    "device_id": device_id,
                    "config_entry_id": config_entry_id,
                }

                remove_result = await client.send_websocket_message(remove_message)
                removal_results.append(
                    {
                        "config_entry_id": config_entry_id,
                        "success": remove_result.get("success", False),
                        "error": (
                            remove_result.get("error")
                            if not remove_result.get("success")
                            else None
                        ),
                    }
                )

            # Check if all removals succeeded
            all_succeeded = all(r["success"] for r in removal_results)
            any_succeeded = any(r["success"] for r in removal_results)

            if all_succeeded:
                return {
                    "success": True,
                    "device_id": device_id,
                    "device_name": device.get("name_by_user") or device.get("name"),
                    "config_entries_removed": len(config_entries),
                    "message": f"Successfully removed device from {len(config_entries)} config entry/entries",
                }
            elif any_succeeded:
                return {
                    "success": True,
                    "partial": True,
                    "device_id": device_id,
                    "device_name": device.get("name_by_user") or device.get("name"),
                    "removal_results": removal_results,
                    "message": "Device partially removed - some config entries could not be removed",
                }
            else:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        "Failed to remove device from any config entries",
                        suggestions=[
                            "Device may be actively managed by its integration. Try disabling it instead.",
                        ],
                        context={
                            "device_id": device_id,
                            "removal_results": removal_results,
                        },
                    )
                )

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error removing device: {e}")
            exception_to_structured_error(
                e,
                context={"device_id": device_id},
            )
