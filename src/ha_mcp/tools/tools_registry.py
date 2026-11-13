"""
Device Registry management tools for Home Assistant.

This module provides tools for managing devices (list, get details, update, remove).

Important: Device renaming does NOT cascade to entities - they are independent registries.
"""

import logging
from typing import Annotated, Any, Literal, NoReturn

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..errors import ErrorCode, create_error_response
from ..utils.device_registry_semantics import (
    EFFECTIVE_AREA_MARKER,
    annotate_device_rows_with_effective_area,
)
from .auto_backup import with_auto_backup
from .coercion import JSON_STRING_COERCION, parse_string_list_param
from .component_api import UNKNOWN_COMMAND_CODE
from .component_devices import (
    fetch_device_list_via_component,
    fetch_device_via_component,
)
from .config_helpers.registry import validate_registry_ids
from .device_enrichment import (
    enrich_matter_diagnostics,
    enrich_zha_metrics,
    enrich_zwave_status,
)
from .helpers import (
    WHITESPACE_CLEARS_NOTE,
    clearable_value,
    exception_to_structured_error,
    log_tool_usage,
    raise_core_refusal,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
)
from .response_helpers import build_pagination_metadata
from .tool_hints import read_only_hints, write_hints

logger = logging.getLogger(__name__)


def _process_device_domain(
    domain: str, value: str, ieee_address: str | None, is_z2m: bool
) -> tuple[str | None, bool]:
    """Return updated (ieee_address, is_z2m) for a single identifier domain."""
    # ZHA: identifier is ["zha", "IEEE_ADDRESS"]
    if domain == "zha":
        return value, is_z2m
    # Z2M: identifier is ["mqtt", "zigbee2mqtt_0xIEEE"] or "zigbee2mqtt_bridge_0xIEEE"
    if domain == "mqtt" and "zigbee2mqtt" in value.lower():
        extracted = (
            "0x" + value.rsplit("_0x", maxsplit=1)[-1]
            if "_0x" in value
            else ieee_address
        )
        return extracted, True
    return ieee_address, is_z2m


def _extract_zigbee_info(identifiers: list[Any]) -> tuple[str | None, bool]:
    """Parse device identifiers into (ieee_address, is_z2m)."""
    ieee_address: str | None = None
    is_z2m = False

    for identifier in identifiers:
        if isinstance(identifier, (list, tuple)) and len(identifier) >= 2:
            domain = identifier[0]
            if isinstance(domain, str):
                ieee_address, is_z2m = _process_device_domain(
                    domain, str(identifier[1]), ieee_address, is_z2m
                )

    return ieee_address, is_z2m


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


def _is_z2m_bridge(device: dict[str, Any]) -> bool:
    """Whether the device carries Zigbee2MQTT's bridge identifier
    ``("mqtt", "zigbee2mqtt_bridge_<ieee>")``."""
    return any(
        isinstance(i, (list, tuple))
        and len(i) >= 2
        and i[0] == "mqtt"
        and str(i[1]).startswith("zigbee2mqtt_bridge_")
        for i in device.get("identifiers") or []
    )


async def _entry_domains(client: Any) -> dict[str, str]:
    """Config-entry id → integration domain, the frontend's device-page lookup."""
    result = await client.send_websocket_message({"type": "config_entries/get"})
    if not result.get("success"):
        # Without the domains every integration read would come back empty,
        # a negative answer the caller could not tell from a real one.
        raise_core_refusal("Failed to read config entries", result)
    return {e["entry_id"]: e["domain"] for e in result.get("result") or []}


# HA-MCP's response-size policy for a ``summary`` list, not a copy of a Core
# rule: the fields of Core's device row that say who the device is, where it
# sits and what it hangs off. ``full`` and a single-device lookup return the
# whole row, so every field stays reachable.
_SUMMARY_FIELDS = frozenset(
    {
        "id",
        "name",
        "name_by_user",
        "manufacturer",
        "model",
        "area_id",
        "disabled_by",
        "parent_device_id",
        "via_device_id",
    }
)


def _get_device_info(
    device: dict[str, Any], domains: dict[str, str], summary: bool = False
) -> dict[str, Any]:
    """Core's device row (in a summary, only its ``_SUMMARY_FIELDS``) plus the
    effective area, the owning integration and radio hints.

    ``area_id`` is the device's own area; ``effective_area_id`` is the one Core
    places it in, where a child device without its own area sits in its
    parent's.
    """
    ieee_address, is_z2m = _extract_zigbee_info(device.get("identifiers") or [])
    ieee_address = _first_ieee_from_connections(
        device.get("connections") or [], ieee_address
    )
    integration_type = (
        "zigbee2mqtt" if is_z2m else domains.get(device["config_entry_id"])
    )
    if summary:
        row = {k: v for k, v in device.items() if k in _SUMMARY_FIELDS}
    else:
        row = {k: v for k, v in device.items() if k != EFFECTIVE_AREA_MARKER}
    device_info: dict[str, Any] = {
        **row,
        "effective_area_id": device.get(EFFECTIVE_AREA_MARKER, device.get("area_id")),
        "integration_type": integration_type,
    }

    if ieee_address:
        device_info["ieee_address"] = ieee_address
    if is_z2m and not _is_z2m_bridge(device):
        # Core's ``name`` is the friendly name Zigbee2MQTT discovered the device
        # with, and its topics use it; ``name_by_user`` stays in Home Assistant.
        # The bridge publishes under ``<base_topic>/bridge/...``, not its name,
        # so it gets no hint.
        friendly_name = device.get("name")
        device_info["friendly_name"] = friendly_name
        device_info["mqtt_topic_hint"] = f"zigbee2mqtt/{friendly_name}/..."
    if integration_type == "zha" and ieee_address:
        device_info["zha_trigger_hint"] = (
            f"Use ieee '{ieee_address}' for zha_event triggers"
        )
    return device_info


def _build_entity_maps(
    all_entities: list[dict[str, Any]], need_full: bool
) -> tuple[dict[str, str], dict[str, list[dict[str, Any]]]]:
    """Build entity→device and device→entities lookup maps; the entities are
    Core's entity-registry rows.

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
                device_to_entities.setdefault(did, []).append(e)
    return entity_to_device, device_to_entities


async def _fetch_entity_rows(client: Any) -> list[dict[str, Any]]:
    """Entity-registry rows for the device<->entity maps; tolerant ([] on failure).

    ``device_list`` carries no per-device entity join (only ``device_get`` does),
    so the full-detail list and the single-device legacy fallback still read the
    whole ``config/entity_registry/list``. Entity-registry failure is non-fatal
    (the caller just loses the entity list / hidden filter), matching the prior
    concurrent-fetch behaviour.
    """
    entity_result = await client.send_websocket_message(
        {"type": "config/entity_registry/list"}
    )
    return entity_result.get("result", []) if entity_result.get("success") else []


async def _strict_entity_rows(client: Any) -> list[dict[str, Any]]:
    """Full entity registry; a failed read raises with :func:`ws_failure_code`'s code.

    The device-id resolver's fallback read. Unlike the tolerant
    :func:`_fetch_entity_rows` (whose callers treat a missing entity list as a
    non-fatal degrade), a failed read here MUST raise rather than collapse to
    ``[]`` — otherwise the resolver would misreport a real entity as
    ENTITY_NOT_FOUND when the read itself failed (a double transient failure:
    the targeted ``get`` failed, then this fallback list failed too).
    """
    entity_result = await client.send_websocket_message(
        {"type": "config/entity_registry/list"}
    )
    if not entity_result.get("success"):
        raise_core_refusal("Failed to access entity registry", entity_result)
    return list(entity_result.get("result", []))


async def _legacy_device_rows(client: Any) -> list[dict[str, Any]]:
    """Full device registry via the legacy ``config/device_registry/list``.

    A failed read raises with :func:`ws_failure_code`'s code, preserving the
    prior fatal-on-device-registry-failure contract.
    """
    list_result = await client.send_websocket_message(
        {"type": "config/device_registry/list"}
    )
    if not list_result.get("success"):
        raise_core_refusal("Failed to access device registry", list_result)
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
        return annotate_device_rows_with_effective_area(list(result.get("devices", [])))
    return annotate_device_rows_with_effective_area(await _legacy_device_rows(client))


# HA core replies with this error code (ERR_NOT_FOUND) from
# ``config/entity_registry/get`` for a genuinely unknown entity. Any other failure
# code is a transient / structural WS problem, NOT HA's "unknown entity" verdict.
_ENTITY_NOT_FOUND_CODE = "not_found"


async def _resolve_device_id_via_legacy(client: Any, entity_id: str) -> str:
    """Resolve entity_id → device_id via the full ``config/entity_registry/list``.

    The whole-registry scan :func:`_resolve_device_id_for_entity` replaced, kept as
    the fallback for a transient failure of the targeted read so a real entity is
    never misreported as nonexistent. Uses the STRICT list read so a failure of
    THIS fallback raises rather than returning an empty list that would itself
    misreport the entity as ENTITY_NOT_FOUND. Raises ENTITY_NOT_FOUND only
    when the read succeeds and the entity is absent or carries no device (the same
    contract).
    """
    entity_to_device, _ = _build_entity_maps(
        await _strict_entity_rows(client), need_full=False
    )
    device_id = entity_to_device.get(entity_id)
    if device_id:
        return device_id
    raise_tool_error(
        create_error_response(
            ErrorCode.ENTITY_NOT_FOUND,
            f"Entity '{entity_id}' not found or has no associated device",
            suggestions=["Use ha_search() to find valid entity IDs"],
            context={"entity_id": entity_id},
        )
    )
    raise AssertionError  # unreachable: raise_tool_error is NoReturn


async def _resolve_device_id_for_entity(client: Any, entity_id: str) -> str:
    """Resolve an entity_id to its device_id via a single native entity read.

    Uses HA's targeted ``config/entity_registry/get`` (one entity, no
    whole-registry dump) rather than scanning the full entity registry. HA returns
    ``success: false`` both for a genuinely unknown entity (error code
    ``not_found``) AND for a transient failure, so the two are told apart by the
    structured error code: ``not_found`` (or a successful read with no device)
    raises ENTITY_NOT_FOUND — the same contract the old full-map lookup produced —
    while any OTHER failure falls back to the legacy whole-registry read so a WS
    hiccup never misreports a real entity as nonexistent.
    """
    result = await client.send_websocket_message(
        {"type": "config/entity_registry/get", "entity_id": entity_id}
    )
    if result.get("success"):
        device_id = (result.get("result") or {}).get("device_id")
        if device_id:
            return str(device_id)
    elif result.get("error_code") != _ENTITY_NOT_FOUND_CODE:
        return await _resolve_device_id_via_legacy(client, entity_id)
    raise_tool_error(
        create_error_response(
            ErrorCode.ENTITY_NOT_FOUND,
            f"Entity '{entity_id}' not found or has no associated device",
            suggestions=["Use ha_search() to find valid entity IDs"],
            context={"entity_id": entity_id},
        )
    )
    raise AssertionError  # unreachable: raise_tool_error is NoReturn


async def _single_device_and_entities(
    client: Any, device_id: str
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """(``all_devices``, ``device_to_entities``) for a single-device lookup.

    When the component serves ``device_get`` and the device exists, returns
    ``[device]`` plus its entity map — both from ONE ``device_get(include_entities)``
    frame, so neither the device NOR the entity registry is dumped. Falls back to
    the legacy full device + entity registries (so a not-found error can still
    list valid ids and the device's entities are still listed) when the device
    is absent (component said ``None``), the component can't serve device_get, or
    the component served the device but not the additive entities half (no
    ``entities`` key — an older device_get predating ``include_entities``): the
    join's absence degrades to legacy rather than reporting a device with zero
    entities.
    """
    result = await fetch_device_via_component(client, device_id, include_entities=True)
    if result is not None:
        device = result.get("device")
        entities = result.get("entities")
        if isinstance(device, dict) and isinstance(entities, list):
            device = dict(device)
            if "effective_area_id" in result:
                device[EFFECTIVE_AREA_MARKER] = result.get("effective_area_id")
            _, device_to_entities = _build_entity_maps(entities, need_full=True)
            return [device], device_to_entities
        # Either an authoritative not-found (device is None) — fall through so the
        # error carries available_device_ids — or a component that served
        # device_get without the entities half. Both take the legacy device AND
        # entity registries.
    # Device registry first, then entity registry — the legacy wire order,
    # which the #1297 error-contract test pins with an ordered mock.
    all_devices = annotate_device_rows_with_effective_area(
        await _legacy_device_rows(client)
    )
    all_entities = await _fetch_entity_rows(client)
    _, device_to_entities = _build_entity_maps(all_entities, need_full=True)
    return all_devices, device_to_entities


async def _raise_device_not_found(
    client: Any, device_id: str, all_rows: list[dict[str, Any]]
) -> NoReturn:
    """Report an unknown device id. A composite id Home Assistant 2026.8 split
    into one device per config entry gets Core's ``split_ids`` and
    ``primary_id``, the map the frontend's device pickers re-point such an id
    with."""
    context: dict[str, Any] = {
        "device_id": device_id,
        "available_device_ids": [d.get("id") for d in all_rows[:10] if d.get("id")],
    }
    suggestions = ["Use ha_get_device() to find valid device IDs"]
    splits = await client.send_websocket_message(
        {"type": "config/device_registry/list_composite_splits"}
    )
    if not splits.get("success"):
        raise_core_refusal("Failed to read composite device splits", splits, context)
    if split := splits["result"].get(device_id):
        context.update(split)
        suggestions = [
            "Home Assistant split this device into one device per config entry: "
            "use one of split_ids; primary_id, when not null, is the device that "
            "took over its former primary config entry"
        ]
    raise_tool_error(
        create_error_response(
            ErrorCode.RESOURCE_NOT_FOUND,
            f"Device not found: {device_id}",
            suggestions=suggestions,
            context=context,
        )
    )


async def _lookup_device_for_remove(client: Any, device_id: str) -> dict[str, Any]:
    """Return the raw device row for ``ha_remove_device``; raise if it does not exist.

    Uses the component's ``device_get`` for the single-device read when available
    (no whole-registry dump), falling back to the legacy full list. An unknown id
    raises through :func:`_raise_device_not_found`, which lists the first ten
    ids of that list and reads Core's composite splits.
    """
    result = await fetch_device_via_component(client, device_id)
    if result is not None:
        device = result.get("device")
        if isinstance(device, dict):
            return device
    all_rows = await _legacy_device_rows(client)
    device = next((d for d in all_rows if d.get("id") == device_id), None)
    if device is None:
        await _raise_device_not_found(client, device_id, all_rows)
    return device


async def _get_single_device_result(
    client: Any,
    device_id: str,
    entity_id: str | None,
    all_devices: list[dict[str, Any]],
    device_to_entities: dict[str, list[dict[str, Any]]],
    domains: dict[str, str],
) -> dict[str, Any]:
    """Return the full-detail single-device response dict."""
    device = next((d for d in all_devices if d.get("id") == device_id), None)
    if not device:
        await _raise_device_not_found(client, device_id, all_devices)

    device_info = _get_device_info(device, domains)
    device_info["entities"] = device_to_entities.get(device_id, [])
    warnings: list[str] = []

    # The device page's Linked devices card: devices of other config entries
    # sharing an identifier or connection (none for a child).
    linked = await client.send_websocket_message(
        {"type": "config/device_registry/list_linked_devices", "device_id": device_id}
    )
    if not linked.get("success"):
        raise_core_refusal(
            "Failed to read linked devices", linked, {"device_id": device_id}
        )
    device_info["linked_devices"] = linked["result"]["linked_devices"]

    if device_info.get("integration_type") == "zha" and device_info.get("ieee_address"):
        await enrich_zha_metrics(client, device_info, warnings)
    if device_info.get("integration_type") == "zwave_js":
        await enrich_zwave_status(client, device_id, device_info, warnings)
    if device_info.get("integration_type") == "matter":
        await enrich_matter_diagnostics(client, device_id, device_info, warnings)

    response: dict[str, Any] = {
        "success": True,
        "device": device_info,
        "entity_count": len(device_info["entities"]),
        "queried_by": "entity_id" if entity_id else "device_id",
        "queried_entity_id": entity_id,
    }
    if warnings:
        # Top-level list[str] per the response contract — never nested under
        # ``device``, where a caller reading the payload would miss it.
        response["warnings"] = warnings
    return response


def _filter_devices(
    all_devices: list[dict[str, Any]],
    domains: dict[str, str],
    area_id: str | None,
    manufacturer_lower: str | None,
    integration_lower: str | None,
    device_to_entities: dict[str, list[dict[str, Any]]],
    detail_level: Literal["summary", "full"],
) -> list[dict[str, Any]]:
    """Filter devices for list mode and optionally attach entity lists."""
    matched: list[dict[str, Any]] = []
    for device in all_devices:
        effective_area = device.get(EFFECTIVE_AREA_MARKER, device.get("area_id"))
        if area_id and effective_area != area_id:
            continue
        device_man = (device.get("manufacturer") or "").lower()
        if manufacturer_lower and manufacturer_lower not in device_man:
            continue
        device_info = _get_device_info(device, domains, detail_level == "summary")
        # "mqtt" also matches Zigbee2MQTT devices, whose entry is MQTT's.
        if integration_lower and integration_lower not in (
            device_info["integration_type"],
            domains.get(device["config_entry_id"]),
        ):
            continue
        if detail_level == "full":
            device_info["entities"] = device_to_entities.get(device.get("id") or "", [])
        matched.append(device_info)
    return matched


def _add_integration_hints(
    result: dict[str, Any],
    integration_lower: str | None,
    all_devices: list[dict[str, Any]],
) -> None:
    """Add Z2M bridge info and integration-specific usage hints to result in-place.

    The bridge is looked up among all devices, so area and manufacturer filters
    do not hide it.
    """
    if integration_lower == "zigbee2mqtt":
        bridge = next((d for d in all_devices if _is_z2m_bridge(d)), None)
        if bridge:
            result["bridge"] = {
                "id": bridge.get("id"),
                "name": bridge.get("name"),
                "ieee_address": _extract_zigbee_info(bridge["identifiers"])[0],
            }
        result["usage_hint"] = (
            "Use 'friendly_name' for MQTT topics: zigbee2mqtt/{friendly_name}/action"
        )
    elif integration_lower == "zha":
        result["usage_hint"] = (
            "Use 'ieee_address' for zha_event triggers in automations"
        )
    elif integration_lower == "zwave_js":
        result["usage_hint"] = (
            "Single device lookup includes Z-Wave JS's node_id and node status "
            "(security, routing)."
        )


def _list_devices_result(
    all_devices: list[dict[str, Any]],
    domains: dict[str, str],
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
        domains,
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

    _add_integration_hints(result, integration_lower, all_devices)
    return result


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
                name = clearable_value(name, "name")
                message["name_by_user"] = name
                updates_made.append(f"name='{name}'" if name else "name cleared")

            if area_id is not None:
                area_id = clearable_value(area_id, "area_id")
                message["area_id"] = area_id
                updates_made.append(
                    f"area_id='{area_id}'" if area_id else "area cleared"
                )

            if disabled_by is not None:
                disabled_by = clearable_value(
                    disabled_by,
                    "disabled_by",
                    hint="Pass 'user' to disable the device, or '' (a single space "
                    "' ' if your client cannot send an empty string) to enable it",
                )
                message["disabled_by"] = disabled_by
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

            # Issue #2159: HA stores an unknown area_id or label_id on the
            # device verbatim. Device labels replace the existing set, so the
            # supplied list is the full set to check; an empty list clears.
            await validate_registry_ids(
                self._client, area_id, labels, None, fail_closed=True
            )

            logger.info(f"Updating device {device_id}: {', '.join(updates_made)}")
            result = await self._client.send_websocket_message(message)

            if not result.get("success"):
                # An unknown id: Core 2026.9+ answers not_found; older Core
                # answers unknown_error, which newer Core also uses for other
                # failures.
                unknown = result.get("error_code") in ("not_found", "unknown_error")
                raise_core_refusal(
                    "Failed to update device",
                    result,
                    {"device_id": device_id},
                    ["Verify the device_id exists using ha_get_device()"]
                    if unknown
                    else None,
                )
            return {
                "success": True,
                "device_id": device_id,
                "updates": updates_made,
                "device_entry": result.get("result"),
                "message": f"Device updated: {', '.join(updates_made)}",
                "note": "Remember: Device rename does NOT cascade to entities. Use ha_set_entity(new_entity_id=...) to rename entities.",
            }

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error updating device: {e}")
            exception_to_structured_error(
                e,
                context={"device_id": device_id},
            )
            return None  # unreachable: exception_to_structured_error always raises

    @tool(
        name="ha_get_device",
        tags={"Device Registry", "Zigbee", "Z-Wave", "Matter"},
        annotations=read_only_hints(
            "Get Device (incl. Zigbee/ZHA/Z2M, Z-Wave and Matter)", open_world=False
        ),
    )
    @log_tool_usage
    async def ha_get_device(
        self,
        device_id: Annotated[
            str | None,
            Field(
                description="Device ID to retrieve details for.",
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
                description=(
                    "Filter by the integration that owns the device (its config "
                    "entry's domain), e.g. 'zha', 'zwave_js', 'mqtt', 'hue'; "
                    "'zigbee2mqtt' selects the Zigbee2MQTT devices within MQTT."
                ),
                default=None,
            ),
        ] = None,
        area_id: Annotated[
            str | None,
            Field(
                description=(
                    "Filter devices by area ID (e.g., 'living_room'); matches "
                    "effective_area_id, so a child device without an area of "
                    "its own matches its parent's"
                ),
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
                description="Max devices to return per page in list mode",
            ),
        ] = 50,
        offset: Annotated[
            int,
            Field(
                default=0,
                ge=0,
                description="Number of devices to skip for pagination",
            ),
        ] = 0,
        detail_level: Annotated[
            Literal["summary", "full"],
            Field(
                default="summary",
                description=(
                    "'summary': the main fields of each device's registry row "
                    "(id, names, manufacturer, model, areas, disabled_by, parent "
                    "and via device) with integration and radio hints. 'full': "
                    "the whole registry row with the same hints, plus the "
                    "device's entity-registry rows. List mode only; a "
                    "single-device lookup ignores it."
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

        **Single device lookup (Core's whole row, its entity rows and linked_devices):**
        - By device_id: ha_get_device(device_id="abc123")
        - By entity_id: ha_get_device(entity_id="light.living_room")

        **Zigbee:** integration="zha" or "zigbee2mqtt". Returns ieee_address, radio metrics.
        **Z-Wave:** integration="zwave_js". Single device lookup returns
        Z-Wave JS's node_id and node_status.
        **Matter:** integration="matter". Returns node_diagnostics (network type,
        reachability, IPs, fabrics). For management use ha_manage_radio.
        """
        try:
            # entity_id -> device_id is a single native entity read (no dump).
            if entity_id and not device_id:
                device_id = await _resolve_device_id_for_entity(self._client, entity_id)

            if device_id:
                # Single-device lookup: the device AND its entities come from one
                # component device_get(include_entities) frame — no device- or
                # entity-registry dump — or the legacy registries when the
                # component can't serve it.
                all_devices, device_to_entities = await _single_device_and_entities(
                    self._client, device_id
                )
                return await _get_single_device_result(
                    self._client,
                    device_id,
                    entity_id,
                    all_devices,
                    device_to_entities,
                    await _entry_domains(self._client),
                )

            # List mode: the device rows come from the component's device_list (or
            # legacy list). The entity registry is read only for the full-detail
            # list (a bulk device->entity map, where one dump beats N joins) and
            # skipped for a summary list.
            need_full = detail_level == "full"
            all_entities = await _fetch_entity_rows(self._client) if need_full else []
            _, device_to_entities = _build_entity_maps(all_entities, need_full)
            all_devices = await _fetch_device_rows(self._client)
            return _list_devices_result(
                all_devices,
                await _entry_domains(self._client),
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
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error getting device: {e}")
            exception_to_structured_error(e)
            return None  # unreachable: exception_to_structured_error always raises

    @tool(
        name="ha_set_device",
        tags={"Device Registry"},
        annotations=write_hints(
            "Set Device", destructive=True, idempotent=True, open_world=False
        ),
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
                description=(
                    "New display name for the device (sets name_by_user). '' clears "
                    "name_by_user and reverts to the integration-provided name. "
                    + WHITESPACE_CLEARS_NOTE
                ),
                default=None,
            ),
        ] = None,
        area_id: Annotated[
            str | None,
            Field(
                description=(
                    "Area/room ID to assign the device to. Use empty string '' to "
                    "unassign. " + WHITESPACE_CLEARS_NOTE
                ),
                default=None,
            ),
        ] = None,
        disabled_by: Annotated[
            str | None,
            Field(
                description=(
                    "Set to 'user' to disable the device, or '' (empty string) "
                    "to enable it. Omit to leave the disabled state unchanged. "
                    + WHITESPACE_CLEARS_NOTE
                ),
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
        # ``ha_remove_device`` guard.
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
        annotations=write_hints(
            "Remove Device", destructive=True, idempotent=True, open_world=False
        ),
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

        Works only when the integration supports removing devices.

        EXAMPLES:
        - Remove orphaned device: ha_remove_device("abc123def456")

        NOTE: For most use cases, consider disabling the device instead:
        ha_set_device(device_id="abc123", disabled_by="user")
        """
        try:
            # Empty/whitespace device_id would surface as a generic
            # "Device not found: " error after a wasted lookup round-trip.
            validate_identifier_not_empty(
                device_id,
                "device_id",
                suggestions=["Use ha_get_device() to find valid device IDs"],
            )
            device = await _lookup_device_for_remove(self._client, device_id)
            result = await self._client.send_websocket_message(
                {"type": "config/device_registry/remove", "device_id": device_id},
                expected_error_codes=frozenset({UNKNOWN_COMMAND_CODE}),
            )
            if result.get("error_code") == UNKNOWN_COMMAND_CODE:
                # Core before 2026.9 has no ``remove``; its predecessor detaches
                # the device's one config entry, which removes the device.
                result = await self._client.send_websocket_message(
                    {
                        "type": "config/device_registry/remove_config_entry",
                        "device_id": device_id,
                        "config_entry_id": device["config_entry_id"],
                    }
                )
            if not result.get("success"):
                raise_core_refusal(
                    "Failed to remove device",
                    result,
                    {"device_id": device_id},
                    [
                        "If the integration does not support removing devices, "
                        "disable the device instead: "
                        "ha_set_device(device_id=..., disabled_by='user')",
                    ],
                )
            return {
                "success": True,
                "device_id": device_id,
                "device_name": device.get("name_by_user") or device.get("name"),
                "message": "Device removed",
            }

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error removing device: {e}")
            exception_to_structured_error(
                e,
                context={"device_id": device_id},
            )
            return None  # unreachable: exception_to_structured_error always raises


def register_registry_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register device registry management tools."""
    register_tool_methods(mcp, RegistryTools(client))
