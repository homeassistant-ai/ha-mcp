"""Zigbee (ZHA) radio handler for ``ha_manage_radio``.

Wraps the Home Assistant ``zha/*`` WebSocket API plus the handful of ZHA
operations that are service-only (``zha.remove``,
``zha.set_zigbee_cluster_attribute``, ``zha.issue_zigbee_cluster_command`` and
``update.install`` for OTA firmware). Service calls go through the REST client's
``call_service`` bridge (the same path ``ha_call_service`` uses).

Node-scoped actions take ``device_id`` and resolve it to the device's Zigbee
IEEE address via the device registry (identifier ``["zha", "<ieee>"]``);
network- and group-scoped actions act on the coordinator.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp.exceptions import ToolError

from ...errors import ErrorCode, create_error_response
from ..helpers import raise_tool_error
from .base import (
    ActionSpec,
    integration_not_found,
    ok,
    resolve_entry_id,
    resolve_update_entity,
    ws_call,
)

logger = logging.getLogger(__name__)

SUPPORTED: dict[str, ActionSpec] = {
    "diagnostics": ActionSpec(
        "Per-node ZHA diagnostics: LQI, RSSI, availability, last-seen, "
        "neighbors and routes.",
        required=("device_id",),
    ),
    "network_status": ActionSpec(
        "ZHA network settings (channel, PAN id, coordinator). Also kicks a "
        "topology scan so the next read reflects current routes."
    ),
    "permit_join": ActionSpec(
        "Open the Zigbee network for joining for `duration` seconds (default "
        "60). Pass a device_id to permit via a specific router."
    ),
    "remove_device": ActionSpec(
        "Remove (leave) a Zigbee node from the network.",
        destructive=True,
        required=("device_id",),
    ),
    "reconfigure": ActionSpec(
        "Re-interview a Zigbee node and re-apply its bindings/reporting.",
        required=("device_id",),
    ),
    "group_add": ActionSpec(
        "Create a Zigbee group. Optional group_id and members "
        "([{ieee, endpoint_id}, ...]).",
        required=("group_name",),
    ),
    "group_remove": ActionSpec(
        "Delete Zigbee groups by id (group_ids: list[int]).",
        destructive=True,
        required=("group_ids",),
    ),
    "group_members_add": ActionSpec(
        "Add members ([{ieee, endpoint_id}, ...]) to a Zigbee group.",
        required=("group_id", "members"),
    ),
    "group_members_remove": ActionSpec(
        "Remove members ([{ieee, endpoint_id}, ...]) from a Zigbee group.",
        required=("group_id", "members"),
    ),
    "bind": ActionSpec(
        "Create a binding from source_ieee to target_ieee (direct device-to-"
        "device control).",
        required=("source_ieee", "target_ieee"),
    ),
    "unbind": ActionSpec(
        "Remove the binding from source_ieee to target_ieee.",
        destructive=True,
        required=("source_ieee", "target_ieee"),
    ),
    "cluster_read": ActionSpec(
        "Read a Zigbee cluster attribute (endpoint_id, cluster_id, attribute; "
        "cluster_type defaults to 'in').",
        required=("device_id", "endpoint_id", "cluster_id", "attribute"),
    ),
    "cluster_write": ActionSpec(
        "Write a Zigbee cluster attribute (endpoint_id, cluster_id, attribute, "
        "value; cluster_type defaults to 'in').",
        destructive=True,
        required=("device_id", "endpoint_id", "cluster_id", "attribute", "value"),
    ),
    "cluster_command": ActionSpec(
        "Issue a Zigbee cluster command (endpoint_id, cluster_id, command, "
        "command_type; optional params/args; cluster_type defaults to 'in').",
        destructive=True,
        required=("device_id", "endpoint_id", "cluster_id", "command", "command_type"),
    ),
    "network_backup": ActionSpec(
        "Create a ZHA network backup (coordinator + network key material)."
    ),
    "network_restore": ActionSpec(
        "Restore a ZHA network from a backup payload.",
        destructive=True,
        required=("backup",),
    ),
    "change_channel": ActionSpec(
        "Move the ZHA network to a new Zigbee channel (new_channel: 11-26).",
        destructive=True,
        required=("new_channel",),
    ),
    "firmware_update": ActionSpec(
        "Install the available OTA firmware on a Zigbee node (via its update.* "
        "entity).",
        long_running=True,
        required=("device_id",),
    ),
}


async def _resolve_ieee(client: Any, device_id: Any) -> str:
    """Resolve a ``device_id`` to its ZHA IEEE address via the device registry.

    Parses the ``["zha", "<ieee>"]`` registry identifier (falling back to an
    ``("ieee", ...)`` connection). Raises VALIDATION_INVALID_PARAMETER when the
    device is not a ZHA device.
    """
    devices = await ws_call(
        client, "config/device_registry/list", context={"device_id": device_id}
    )
    for device in devices or []:
        if device.get("id") != device_id:
            continue
        for ident in device.get("identifiers", []):
            if (
                isinstance(ident, (list, tuple))
                and len(ident) >= 2
                and ident[0] == "zha"
            ):
                return str(ident[1])
        for conn in device.get("connections", []):
            if isinstance(conn, (list, tuple)) and len(conn) >= 2 and conn[0] == "ieee":
                return str(conn[1])
        break
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"Device '{device_id}' is not a ZHA Zigbee device (no IEEE address found)",
            context={"device_id": device_id, "radio": "zigbee"},
            suggestions=[
                "Pass a ZHA device_id (ha_get_device reports integration_type 'zha')",
                "Zigbee2MQTT devices are not managed here — use the MQTT/Z2M tools",
            ],
        )
    )
    raise AssertionError  # py/mixed-returns terminal: raise_tool_error is NoReturn


async def _call_service(client: Any, domain: str, service: str, **data: Any) -> Any:
    """Call an HA service via the REST client, dropping ``None`` fields."""
    payload = {k: v for k, v in data.items() if v is not None}
    return await client.call_service(domain, service, payload)


async def handle(client: Any, action: str, args: dict[str, Any]) -> dict[str, Any]:
    """Execute one Zigbee/ZHA action (validation/confirm already applied by caller)."""
    device_id = args.get("device_id")
    cluster_type = args.get("cluster_type", "in")

    # --- read -------------------------------------------------------------- #
    if action == "diagnostics":
        ieee = await _resolve_ieee(client, device_id)
        diagnostics = await ws_call(
            client, "zha/device", ieee=ieee, context={"ieee": ieee}
        )
        return ok("zigbee", "diagnostics", ieee=ieee, diagnostics=diagnostics)

    if action == "network_status":
        entry_id = await resolve_entry_id(client, "zha")
        if not entry_id:
            return integration_not_found("zigbee", "zha")
        network = await ws_call(client, "zha/network/settings")
        try:
            await ws_call(client, "zha/topology/update")
        except ToolError as exc:
            # Topology refresh is best-effort: a command-level failure (the
            # ToolError ws_call raises on success=False) leaves routes stale but
            # must not fail an otherwise-successful network_status read. Transport
            # errors are normalized to that ToolError by send_websocket_message;
            # anything unexpected (a real bug) still propagates.
            logger.debug("zha/topology/update refresh failed (non-fatal): %s", exc)
        return ok("zigbee", "network_status", config_entry_id=entry_id, network=network)

    if action == "cluster_read":
        ieee = await _resolve_ieee(client, device_id)
        value = await ws_call(
            client,
            "zha/devices/clusters/attributes/value",
            ieee=ieee,
            endpoint_id=args.get("endpoint_id"),
            cluster_id=args.get("cluster_id"),
            cluster_type=cluster_type,
            attribute=args.get("attribute"),
            manufacturer=args.get("manufacturer"),
            context={"ieee": ieee, "cluster_id": args.get("cluster_id")},
        )
        return ok("zigbee", "cluster_read", ieee=ieee, value=value)

    # --- node management --------------------------------------------------- #
    if action == "permit_join":
        duration = args.get("duration", 60)
        permit_ieee = await _resolve_ieee(client, device_id) if device_id else None
        result = await ws_call(
            client,
            "zha/devices/permit",
            duration=duration,
            ieee=permit_ieee,
            context={"duration": duration, "ieee": permit_ieee},
        )
        return ok(
            "zigbee", "permit_join", duration=duration, ieee=permit_ieee, result=result
        )

    if action == "reconfigure":
        ieee = await _resolve_ieee(client, device_id)
        result = await ws_call(
            client, "zha/devices/reconfigure", ieee=ieee, context={"ieee": ieee}
        )
        return ok("zigbee", "reconfigure", ieee=ieee, result=result)

    if action == "remove_device":
        ieee = await _resolve_ieee(client, device_id)
        result = await _call_service(client, "zha", "remove", ieee=ieee)
        return ok("zigbee", "remove_device", ieee=ieee, result=result)

    if action == "firmware_update":
        entity_id = await resolve_update_entity(client, device_id, platform="zha")
        result = await _call_service(client, "update", "install", entity_id=entity_id)
        return ok(
            "zigbee",
            "firmware_update",
            entity_id=entity_id,
            result=result,
            long_running=True,
        )

    # --- groups ------------------------------------------------------------ #
    if action == "group_add":
        group = await ws_call(
            client,
            "zha/group/add",
            group_name=args.get("group_name"),
            group_id=args.get("group_id"),
            members=args.get("members"),
            context={"group_name": args.get("group_name")},
        )
        return ok("zigbee", "group_add", group=group)

    if action == "group_remove":
        result = await ws_call(
            client,
            "zha/group/remove",
            group_ids=args.get("group_ids"),
            context={"group_ids": args.get("group_ids")},
        )
        return ok("zigbee", "group_remove", result=result)

    if action == "group_members_add":
        group = await ws_call(
            client,
            "zha/group/members/add",
            group_id=args.get("group_id"),
            members=args.get("members"),
            context={"group_id": args.get("group_id")},
        )
        return ok("zigbee", "group_members_add", group=group)

    if action == "group_members_remove":
        group = await ws_call(
            client,
            "zha/group/members/remove",
            group_id=args.get("group_id"),
            members=args.get("members"),
            context={"group_id": args.get("group_id")},
        )
        return ok("zigbee", "group_members_remove", group=group)

    # --- bindings ---------------------------------------------------------- #
    if action == "bind":
        result = await ws_call(
            client,
            "zha/devices/bind",
            source_ieee=args.get("source_ieee"),
            target_ieee=args.get("target_ieee"),
            context={
                "source_ieee": args.get("source_ieee"),
                "target_ieee": args.get("target_ieee"),
            },
        )
        return ok("zigbee", "bind", result=result)

    if action == "unbind":
        result = await ws_call(
            client,
            "zha/devices/unbind",
            source_ieee=args.get("source_ieee"),
            target_ieee=args.get("target_ieee"),
            context={
                "source_ieee": args.get("source_ieee"),
                "target_ieee": args.get("target_ieee"),
            },
        )
        return ok("zigbee", "unbind", result=result)

    # --- cluster writes (service-only) ------------------------------------- #
    if action == "cluster_write":
        ieee = await _resolve_ieee(client, device_id)
        result = await _call_service(
            client,
            "zha",
            "set_zigbee_cluster_attribute",
            ieee=ieee,
            endpoint_id=args.get("endpoint_id"),
            cluster_id=args.get("cluster_id"),
            cluster_type=cluster_type,
            attribute=args.get("attribute"),
            value=args.get("value"),
            manufacturer=args.get("manufacturer"),
        )
        return ok("zigbee", "cluster_write", ieee=ieee, result=result)

    if action == "cluster_command":
        ieee = await _resolve_ieee(client, device_id)
        result = await _call_service(
            client,
            "zha",
            "issue_zigbee_cluster_command",
            ieee=ieee,
            endpoint_id=args.get("endpoint_id"),
            cluster_id=args.get("cluster_id"),
            cluster_type=cluster_type,
            command=args.get("command"),
            command_type=args.get("command_type"),
            params=args.get("params"),
            args=args.get("args"),
            manufacturer=args.get("manufacturer"),
        )
        return ok("zigbee", "cluster_command", ieee=ieee, result=result)

    # --- network ----------------------------------------------------------- #
    if action == "network_backup":
        backup = await ws_call(client, "zha/network/backups/create")
        return ok("zigbee", "network_backup", backup=backup)

    if action == "network_restore":
        result = await ws_call(
            client,
            "zha/network/backups/restore",
            backup=args.get("backup"),
            context={"radio": "zigbee"},
        )
        return ok("zigbee", "network_restore", result=result)

    if action == "change_channel":
        new_channel = args.get("new_channel")
        result = await ws_call(
            client,
            "zha/network/change_channel",
            new_channel=new_channel,
            context={"new_channel": new_channel},
        )
        return ok("zigbee", "change_channel", new_channel=new_channel, result=result)

    # Unreachable: dispatcher validates action against SUPPORTED first.
    raise AssertionError(f"unhandled zigbee action: {action}")
