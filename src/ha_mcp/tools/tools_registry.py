"""
Device Registry management tools for Home Assistant.

This module provides tools for managing devices (list, get details, update, remove).

Important: Device renaming does NOT cascade to entities - they are independent registries.
"""

import asyncio
import logging
from typing import Annotated, Any, Literal

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..client.rest_client import HomeAssistantAPIError, HomeAssistantConnectionError
from ..errors import ErrorCode, create_error_response
from .auto_backup import with_auto_backup
from .component_devices import (
    fetch_device_list_via_component,
    fetch_device_via_component,
)
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
)
from .util_helpers import (
    JSON_STRING_COERCION,
    build_pagination_metadata,
    parse_string_list_param,
)

logger = logging.getLogger(__name__)


def _process_device_domain(
    domain: str,
    value: str,
    ieee_address: str | None,
    is_z2m: bool,
    zwave_node_id: str | None,
) -> tuple[str | None, bool, str | None]:
    """Return updated (ieee_address, is_z2m, zwave_node_id) for a single identifier domain."""
    # ZHA: identifier is ["zha", "IEEE_ADDRESS"]
    if domain == "zha":
        return value, is_z2m, zwave_node_id
    # Z2M: identifier is ["mqtt", "zigbee2mqtt_0xIEEE"] or "zigbee2mqtt_bridge_0xIEEE"
    if domain == "mqtt" and "zigbee2mqtt" in value.lower():
        extracted = "0x" + value.split("_0x")[-1] if "_0x" in value else ieee_address
        return extracted, True, zwave_node_id
    # Z-Wave JS: identifier is ["zwave_js", "{home_id}-{node_id}"]
    if domain == "zwave_js" and "-" in value:
        return ieee_address, is_z2m, value.split("-")[1]
    return ieee_address, is_z2m, zwave_node_id


def _extract_zigbee_info(
    identifiers: list[Any],
) -> tuple[list[str], str | None, bool, str | None]:
    """Parse device identifiers into (integration_sources, ieee_address, is_z2m, zwave_node_id)."""
    integration_sources: list[str] = []
    ieee_address: str | None = None
    is_z2m = False
    zwave_node_id: str | None = None

    for identifier in identifiers:
        if isinstance(identifier, (list, tuple)) and len(identifier) >= 2:
            domain = identifier[0]
            if isinstance(domain, str):
                if domain not in integration_sources:
                    integration_sources.append(domain)
                ieee_address, is_z2m, zwave_node_id = _process_device_domain(
                    domain,
                    str(identifier[1]),
                    ieee_address,
                    is_z2m,
                    zwave_node_id,
                )

    return integration_sources, ieee_address, is_z2m, zwave_node_id


def _first_ieee_from_connections(
    connections: list[Any], existing_ieee: str | None
) -> str | None:
    """Return existing_ieee if already set; otherwise the first 'ieee' connection value, or None."""
    if not isinstance(connections, list) or existing_ieee:
        return existing_ieee
    for connection in connections:
        if (
            isinstance(connection, (list, tuple))
            and len(connection) >= 2
            and connection[0] == "ieee"
        ):
            return str(connection[1])
    return existing_ieee


def _classify_integration(integration_sources: list[str], is_z2m: bool) -> str:
    """Return the primary integration type string for a device."""
    if "zha" in integration_sources:
        return "zha"
    if is_z2m:
        return "zigbee2mqtt"
    if "zwave_js" in integration_sources:
        return "zwave_js"
    if "mqtt" in integration_sources:
        return "mqtt"
    if integration_sources:
        return integration_sources[0]
    return "unknown"


def _get_device_info(device: dict[str, Any]) -> dict[str, Any]:
    """Extract integration info and build a device summary dict."""
    integration_sources, ieee_address, is_z2m, zwave_node_id = _extract_zigbee_info(
        device.get("identifiers", [])
    )
    ieee_address = _first_ieee_from_connections(
        device.get("connections", []), ieee_address
    )
    integration_type = _classify_integration(integration_sources, is_z2m)
    friendly_name = device.get("name_by_user") or device.get("name")

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

    if ieee_address:
        device_info["ieee_address"] = ieee_address
    if integration_type == "zigbee2mqtt":
        device_info["friendly_name"] = friendly_name
        device_info["mqtt_topic_hint"] = f"zigbee2mqtt/{friendly_name}/..."
    if integration_type == "zha" and ieee_address:
        device_info["zha_trigger_hint"] = (
            f"Use ieee '{ieee_address}' for zha_event triggers"
        )
    if integration_type == "zwave_js" and zwave_node_id:
        device_info["node_id"] = zwave_node_id

    return device_info


def _build_entity_maps(
    all_entities: list[dict[str, Any]], need_full: bool
) -> tuple[dict[str, str], dict[str, list[dict[str, Any]]]]:
    """Build entity→device and device→entities lookup maps.

    device_to_entities is only populated when need_full is True; building it for
    every list-mode request would be wasteful when only entity→device lookup is needed.
    """
    entity_to_device: dict[str, str] = {}
    device_to_entities: dict[str, list[dict[str, Any]]] = {}
    for e in all_entities:
        eid = e.get("entity_id")
        did = e.get("device_id")
        if eid and did:
            entity_to_device[eid] = did
            if need_full:
                device_to_entities.setdefault(did, []).append(
                    {
                        "entity_id": eid,
                        "name": e.get("name") or e.get("original_name"),
                        "platform": e.get("platform"),
                    }
                )
    return entity_to_device, device_to_entities


async def _fetch_entity_rows(client: Any) -> list[dict[str, Any]]:
    """Entity-registry rows for the device<->entity maps; tolerant ([] on failure).

    There is no per-device entity capability, so a device's entity list still
    comes from the full ``config/entity_registry/list``. Entity-registry failure
    is non-fatal (the caller just loses the entity list / hidden filter), matching
    the prior concurrent-fetch behaviour.
    """
    entity_result = await client.send_websocket_message(
        {"type": "config/entity_registry/list"}
    )
    return entity_result.get("result", []) if entity_result.get("success") else []


async def _legacy_device_rows(client: Any) -> list[dict[str, Any]]:
    """Full device registry via the legacy ``config/device_registry/list``.

    Raises SERVICE_CALL_FAILED when the read fails, preserving the prior
    fatal-on-device-registry-failure contract.
    """
    list_result = await client.send_websocket_message(
        {"type": "config/device_registry/list"}
    )
    if not list_result.get("success"):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to access device registry: {list_result.get('error', 'Unknown error')}",
            )
        )
    return list(list_result.get("result", []))


async def _fetch_device_rows(client: Any) -> list[dict[str, Any]]:
    """Full device registry — component ``device_list`` or legacy list.

    Routes through ``ha_mcp_tools/device_list`` when the component serves it (the
    raw ``DeviceEntry`` shape, byte-identical to ``config/device_registry/list``),
    else the legacy WS list. Used for LIST mode, where the whole registry is the
    intended payload.
    """
    result = await fetch_device_list_via_component(client)
    if result is not None:
        return list(result.get("devices", []))
    return await _legacy_device_rows(client)


async def _device_rows_for_single_lookup(
    client: Any, device_id: str
) -> list[dict[str, Any]]:
    """Device rows for a single lookup: just the target, else the full registry.

    When the component serves ``device_get`` and the device exists, returns
    ``[device]`` (one in-process read — no whole-registry dump). Otherwise (device
    absent, or the component can't serve device_get) returns the full registry via
    the LEGACY list so a not-found error can still suggest valid ids — not the
    component ``device_list``, since a device_get that just failed
    ``unknown_command`` invalidated the caps and re-probing for a sibling command
    is pointless churn.
    """
    result = await fetch_device_via_component(client, device_id)
    if result is not None:
        device = result.get("device")
        if isinstance(device, dict):
            return [device]
        # Component authoritative not-found: fall through to the full list so the
        # not-found error carries available_device_ids.
    return await _legacy_device_rows(client)


async def _lookup_device_for_remove(client: Any, device_id: str) -> dict[str, Any]:
    """Return the raw device row for ``ha_remove_device``; raise if it does not exist.

    Uses the component's ``device_get`` for the single-device read when available
    (no whole-registry dump), falling back to the legacy full list — also used to
    build the not-found error's available-ids suggestion.
    """
    result = await fetch_device_via_component(client, device_id)
    if result is not None:
        device = result.get("device")
        if isinstance(device, dict):
            return device
    all_rows = await _legacy_device_rows(client)
    device = next((d for d in all_rows if d.get("id") == device_id), None)
    if device is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Device not found: {device_id}",
                suggestions=["Use ha_get_device() to find valid device IDs"],
                context={
                    "device_id": device_id,
                    "available_device_ids": [
                        d.get("id") for d in all_rows[:10] if d.get("id")
                    ],
                },
            )
        )
        return {}  # unreachable: raise_tool_error always raises
    return device


async def _enrich_zha_metrics(client: Any, device_info: dict[str, Any]) -> None:
    """Fetch ZHA radio metrics (LQI/RSSI) and add to device_info in-place."""
    try:
        zha_result = await client.send_websocket_message({"type": "zha/devices"})
        if zha_result.get("success"):
            zha_by_ieee = {
                d.get("ieee"): d for d in zha_result.get("result", []) if d.get("ieee")
            }
            zha_dev = zha_by_ieee.get(device_info["ieee_address"])
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


async def _enrich_zwave_status(
    client: Any, device_id: str, device_info: dict[str, Any]
) -> None:
    """Fetch Z-Wave node status and add to device_info in-place."""
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
                "highest_security_class": node_data.get("highest_security_class"),
                "zwave_plus_version": node_data.get("zwave_plus_version"),
                "is_controller_node": node_data.get("is_controller_node"),
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


async def _enrich_matter_diagnostics(
    client: Any, device_id: str, device_info: dict[str, Any]
) -> None:
    """Fetch Matter node diagnostics and add to device_info in-place.

    Mirrors _enrich_zwave_status: surfaces the Matter equivalent of Z-Wave node
    status — network type (wifi/thread), reachability, IPs and joined fabrics.
    """
    try:
        result = await client.send_websocket_message(
            {"type": "matter/node_diagnostics", "device_id": device_id}
        )
        if result.get("success"):
            data = result.get("result", {})
            device_info["node_diagnostics"] = {
                "network_type": data.get("network_type"),
                "node_type": data.get("node_type"),
                "available": data.get("available"),
                "network_name": data.get("network_name"),
                # Upstream NodeDiagnostics misspells the field "ip_adresses"
                # (single d); read that key but surface it correctly.
                "ip_addresses": data.get("ip_adresses"),
                "mac_address": data.get("mac_address"),
                "active_fabrics": data.get("active_fabrics"),
                "active_fabric_index": data.get("active_fabric_index"),
            }
    except (
        HomeAssistantConnectionError,
        HomeAssistantAPIError,
        TimeoutError,
        OSError,
    ) as e:
        logger.warning(
            "Could not fetch Matter node diagnostics for device %s: %s",
            device_info.get("device_id"),
            e,
        )


async def _get_single_device_result(
    client: Any,
    device_id: str,
    entity_id: str | None,
    all_devices: list[dict[str, Any]],
    device_to_entities: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Return the full-detail single-device response dict."""
    device = next((d for d in all_devices if d.get("id") == device_id), None)
    if not device:
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Device not found: {device_id}",
                suggestions=["Use ha_get_device() to find valid device IDs"],
                context={
                    "device_id": device_id,
                    "available_device_ids": [
                        d.get("id") for d in all_devices[:10] if d.get("id")
                    ],
                },
            )
        )
        return {}  # unreachable: raise_tool_error always raises

    device_info = _get_device_info(device)
    device_info["entities"] = device_to_entities.get(device_id, [])
    device_info["name_by_user"] = device.get("name_by_user")
    device_info["default_name"] = device.get("name")
    device_info["hw_version"] = device.get("hw_version")
    device_info["serial_number"] = device.get("serial_number")
    device_info["disabled_by"] = device.get("disabled_by")
    device_info["labels"] = device.get("labels", [])
    device_info["config_entries"] = device.get("config_entries", [])
    device_info["connections"] = device.get("connections", [])
    device_info["identifiers"] = device.get("identifiers", [])

    if device_info.get("integration_type") == "zha" and device_info.get("ieee_address"):
        await _enrich_zha_metrics(client, device_info)
    if device_info.get("integration_type") == "zwave_js" and device_info.get("node_id"):
        await _enrich_zwave_status(client, device_id, device_info)
    if device_info.get("integration_type") == "matter":
        await _enrich_matter_diagnostics(client, device_id, device_info)

    entities = device_info.get("entities", [])
    return {
        "success": True,
        "device": device_info,
        "entities": entities,  # Also at top level for backward compatibility
        "entity_count": len(entities),
        "queried_by": "entity_id" if entity_id else "device_id",
        "queried_entity_id": entity_id,
    }


def _matches_integration(
    device_info: dict[str, Any], integration_lower: str, named_types: list[str]
) -> bool:
    """Check whether a device matches the requested integration filter."""
    if integration_lower in named_types:
        return bool(device_info.get("integration_type") == integration_lower)
    return integration_lower in device_info.get("integration_sources", [])


def _filter_devices(
    all_devices: list[dict[str, Any]],
    area_id: str | None,
    manufacturer_lower: str | None,
    integration_lower: str | None,
    device_to_entities: dict[str, list[dict[str, Any]]],
    detail_level: Literal["summary", "full"],
) -> list[dict[str, Any]]:
    """Filter devices for list mode and optionally attach entity lists."""
    named_types = ["zigbee2mqtt", "zha", "zwave_js"]
    matched: list[dict[str, Any]] = []
    for device in all_devices:
        if area_id and device.get("area_id") != area_id:
            continue
        device_man = (device.get("manufacturer") or "").lower()
        if manufacturer_lower and manufacturer_lower not in device_man:
            continue
        device_info = _get_device_info(device)
        if integration_lower and not _matches_integration(
            device_info, integration_lower, named_types
        ):
            continue
        if detail_level == "full":
            device_info["entities"] = device_to_entities.get(device.get("id") or "", [])
        matched.append(device_info)
    return matched


def _add_integration_hints(
    result: dict[str, Any],
    integration_lower: str | None,
    matched_devices: list[dict[str, Any]],
) -> None:
    """Add Z2M bridge info and integration-specific usage hints to result in-place."""
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


def _list_devices_result(
    all_devices: list[dict[str, Any]],
    device_to_entities: dict[str, list[dict[str, Any]]],
    integration: str | None,
    area_id: str | None,
    manufacturer: str | None,
    limit: int,
    offset: int,
    detail_level: Literal["summary", "full"],
) -> dict[str, Any]:
    """Build the paginated device list response."""
    integration_lower = integration.lower() if integration else None
    manufacturer_lower = manufacturer.lower() if manufacturer else None

    matched_devices = _filter_devices(
        all_devices,
        area_id,
        manufacturer_lower,
        integration_lower,
        device_to_entities,
        detail_level,
    )
    total_matched = len(matched_devices)
    paginated = matched_devices[offset : offset + limit]

    result: dict[str, Any] = {
        "success": True,
        **build_pagination_metadata(total_matched, offset, limit, len(paginated)),
        "total_devices": len(all_devices),
        "devices": paginated,
        "detail_level": detail_level,
    }

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

    _add_integration_hints(result, integration_lower, matched_devices)
    return result


async def _remove_device_config_entries(
    client: Any,
    device_id: str,
    config_entries: list[str],
) -> list[dict[str, Any]]:
    """Remove device from each config entry concurrently; returns per-entry results."""
    raw = await asyncio.gather(
        *(
            client.send_websocket_message(
                {
                    "type": "config/device_registry/remove_config_entry",
                    "device_id": device_id,
                    "config_entry_id": config_entry_id,
                }
            )
            for config_entry_id in config_entries
        ),
        return_exceptions=True,
    )
    results: list[dict[str, Any]] = []
    for config_entry_id, r in zip(config_entries, raw, strict=True):
        if isinstance(r, BaseException):
            results.append(
                {"config_entry_id": config_entry_id, "success": False, "error": str(r)}
            )
        else:
            results.append(
                {
                    "config_entry_id": config_entry_id,
                    "success": r.get("success", False),
                    "error": r.get("error") if not r.get("success") else None,
                }
            )
    return results


class RegistryTools:
    """Device registry tools: get, update, and remove devices."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _update_device_internal(
        self,
        device_id: str,
        name: str | None = None,
        area_id: str | None = None,
        disabled_by: str | None = None,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Internal implementation of device update."""
        try:
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
            result = await self._client.send_websocket_message(message)

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
            return None  # unreachable: exception_to_structured_error always raises
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    @tool(
        name="ha_get_device",
        tags={"Device Registry", "Zigbee", "Z-Wave", "Matter"},
        annotations={
            "openWorldHint": False,
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get Device (incl. Zigbee/ZHA/Z2M, Z-Wave and Matter)",
        },
    )
    @log_tool_usage
    async def ha_get_device(
        self,
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
            int,
            Field(
                default=50,
                ge=1,
                le=200,
                description="Max devices to return per page in list mode (default: 50)",
            ),
        ] = 50,
        offset: Annotated[
            int,
            Field(
                default=0,
                ge=0,
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
        **Matter:** integration="matter". Returns node_diagnostics (network type,
        reachability, IPs, fabrics). For management use ha_manage_radio.
        """
        try:
            # A device's entity list (and entity_id -> device resolution) still
            # needs the entity registry; a summary list needs neither, so its
            # entity fetch is skipped. The device rows come from the component's
            # device_get (single lookup) / device_list (list) when available, so a
            # single lookup no longer dumps the whole device registry.
            need_full = bool(device_id or entity_id or detail_level == "full")
            all_entities = await _fetch_entity_rows(self._client) if need_full else []
            entity_to_device, device_to_entities = _build_entity_maps(
                all_entities, need_full
            )

            if entity_id and not device_id:
                resolved = entity_to_device.get(entity_id)
                if not resolved:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.ENTITY_NOT_FOUND,
                            f"Entity '{entity_id}' not found or has no associated device",
                            suggestions=["Use ha_search() to find valid entity IDs"],
                            context={"entity_id": entity_id},
                        )
                    )
                device_id = resolved

            if device_id:
                all_devices = await _device_rows_for_single_lookup(
                    self._client, device_id
                )
                return await _get_single_device_result(
                    self._client, device_id, entity_id, all_devices, device_to_entities
                )
            all_devices = await _fetch_device_rows(self._client)
            return _list_devices_result(
                all_devices,
                device_to_entities,
                integration,
                area_id,
                manufacturer,
                limit,
                offset,
                detail_level,
            )

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error getting device: {e}")
            exception_to_structured_error(e)
            return None  # unreachable: exception_to_structured_error always raises

    @tool(
        name="ha_set_device",
        tags={"Device Registry"},
        annotations={
            "openWorldHint": False,
            "destructiveHint": True,
            "title": "Set Device",
        },
    )
    @with_auto_backup(domain="device", id_param="device_id")
    @log_tool_usage
    async def ha_set_device(
        self,
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
            JSON_STRING_COERCION,
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
        1. ha_set_device(device_id="abc", name="Living Room Sensor")  # Rename device
        2. ha_set_entity("sensor.old", new_entity_id="sensor.living_room")  # Rename entities separately

        PARAMETERS:
        - name: Sets the user-defined display name (name_by_user)
        - area_id: Assigns device to an area/room. Use '' to remove from area.
        - disabled_by: Set to 'user' to disable, or empty to enable
        - labels: List of labels (replaces existing labels)

        EXAMPLES:
        - Rename device: ha_set_device("abc123", name="Living Room Hub")
        - Move to area: ha_set_device("abc123", area_id="living_room")
        - Disable device: ha_set_device("abc123", disabled_by="user")
        - Enable device: ha_set_device("abc123", disabled_by="")
        - Add labels: ha_set_device("abc123", labels=["important", "sensor"])
        """
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

        # Empty/whitespace device_id would reach the
        # ``config/device_registry/update`` WS message inside
        # ``_update_device_internal`` and surface as a misleading HA
        # "device not found" — same destructive-WS-call class as the
        # ``ha_remove_device`` guard added in this PR.
        validate_identifier_not_empty(
            device_id,
            "device_id",
            suggestions=["Use ha_get_device() to find valid device IDs"],
        )
        return await self._update_device_internal(
            device_id=device_id,
            name=name,
            area_id=area_id,
            disabled_by=disabled_by,
            labels=parsed_labels,
        )

    @tool(
        name="ha_remove_device",
        tags={"Device Registry"},
        annotations={
            "openWorldHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "title": "Remove Device",
        },
    )
    @with_auto_backup(domain="device", id_param="device_id")
    @log_tool_usage
    async def ha_remove_device(
        self,
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
        ha_set_device(device_id="abc123", disabled_by="user")
        """
        try:
            # Empty/whitespace device_id would slip past the local-filter dict
            # lookup below and surface as a generic "Device not found: " error
            # after a wasted registry-list round-trip.
            validate_identifier_not_empty(
                device_id,
                "device_id",
                suggestions=["Use ha_get_device() to find valid device IDs"],
            )
            device = await _lookup_device_for_remove(self._client, device_id)

            config_entries = device.get("config_entries", [])
            device_name = device.get("name_by_user") or device.get("name")
            if not config_entries:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        "Device has no config entries - cannot be removed via this method",
                        suggestions=[
                            "This device may be managed by an integration directly. Try disabling it instead.",
                        ],
                        context={"device_id": device_id, "device_name": device_name},
                    )
                )

            removal_results = await _remove_device_config_entries(
                self._client, device_id, config_entries
            )
            all_succeeded = all(r["success"] for r in removal_results)
            any_succeeded = any(r["success"] for r in removal_results)

            if all_succeeded:
                return {
                    "success": True,
                    "device_id": device_id,
                    "device_name": device_name,
                    "config_entries_removed": len(config_entries),
                    "message": f"Successfully removed device from {len(config_entries)} config entry/entries",
                }
            elif any_succeeded:
                return {
                    "success": True,
                    "partial": True,
                    "device_id": device_id,
                    "device_name": device_name,
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
            return None  # unreachable: exception_to_structured_error always raises
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable


def register_registry_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register device registry management tools."""
    register_tool_methods(mcp, RegistryTools(client))
