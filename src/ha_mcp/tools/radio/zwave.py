"""Z-Wave JS radio handler for ``ha_manage_radio``.

Wraps the Home Assistant ``zwave_js/*`` WebSocket API and the ``zwave_js.ping``
service: per-node diagnostics (read), controller/network summary (read),
non-interactive inclusion / exclusion, re-interview, mesh route rebuilds,
configuration-parameter writes, firmware install, and the network-wiping
controller hard reset.

Node-scoped actions key on ``device_id``; controller-scoped actions resolve the
single ``zwave_js`` config entry via ``resolve_entry_id``. Interactive S2 secure
inclusion (the read-the-PIN handshake) is *not* scriptable here — provide
SmartStart/QR/DSK provisioning data, or use the Home Assistant UI.
"""

from __future__ import annotations

from typing import Any

from ...errors import ErrorCode, create_error_response
from ..helpers import raise_tool_error
from .base import (
    ActionSpec,
    integration_not_found,
    integration_required,
    ok,
    require,
    resolve_entry_id,
    resolve_update_entity,
    ws_call,
)

# Cap surfaced node summaries (mirrors ha_get_system_health's zwave_network).
_NODE_LIMIT = 50

# Pre-shared credential forms that make active inclusion non-interactive (no S2
# read-the-PIN handshake). One of these is required for action='add' unless
# SmartStart provisioning is used.
_ADD_CREDENTIALS = (
    "qr_provisioning_information",
    "qr_code_string",
    "planned_provisioning_entry",
    "dsk",
)

SUPPORTED: dict[str, ActionSpec] = {
    "diagnostics": ActionSpec(
        "Per-node Z-Wave status: node_id, status, routing, security class, "
        "Z-Wave Plus version, controller flag.",
        required=("device_id",),
    ),
    "network_status": ActionSpec(
        "Z-Wave controller summary plus per-node status/security/routing "
        "(capped at 50 nodes)."
    ),
    "ping": ActionSpec(
        "Actively ping a Z-Wave node to check it is reachable.",
        required=("device_id",),
    ),
    "add": ActionSpec(
        "Include a Z-Wave node non-interactively via SmartStart provisioning "
        "(params.smart_start=True) or active inclusion with pre-shared "
        "QR/DSK credentials. Interactive S2 read-the-PIN pairing is not "
        "supported here — use the Home Assistant UI for that.",
        long_running=True,
    ),
    "remove_device": ActionSpec(
        "Remove a Z-Wave node: open an exclusion window (default) or "
        "force-remove a known-failed node (params.failed=True).",
        destructive=True,
        long_running=True,
    ),
    "reinterview": ActionSpec(
        "Re-interview a Z-Wave node to refresh its command classes and values.",
        long_running=True,
        required=("device_id",),
    ),
    "rebuild_routes": ActionSpec(
        "Rebuild mesh routes for one node (default) or the whole network "
        "(params.scope='network').",
        long_running=True,
    ),
    "set_config_param": ActionSpec(
        "Set a Z-Wave configuration parameter on a node.",
        required=("device_id", "property", "value"),
    ),
    "firmware_update": ActionSpec(
        "Install a pending firmware update for a Z-Wave node via its update entity.",
        long_running=True,
        required=("device_id",),
    ),
    "hard_reset": ActionSpec(
        "Factory-reset the Z-Wave controller, wiping the entire network.",
        destructive=True,
        long_running=True,
    ),
}


async def _zwave_entry_id(client: Any) -> str | None:
    """Resolve the single ``zwave_js`` config entry_id (None if not configured)."""
    return await resolve_entry_id(client, "zwave_js")


async def handle(client: Any, action: str, args: dict[str, Any]) -> dict[str, Any]:
    """Execute one Z-Wave action (validation/confirm already applied by caller)."""
    device_id = args.get("device_id")

    if action == "diagnostics":
        node = (
            await ws_call(
                client,
                "zwave_js/node_status",
                device_id=device_id,
                context={"device_id": device_id},
            )
            or {}
        )
        node_status = {
            "node_id": node.get("node_id"),
            "status": node.get("status"),
            "is_routing": node.get("is_routing"),
            "is_secure": node.get("is_secure"),
            "highest_security_class": node.get("highest_security_class"),
            "zwave_plus_version": node.get("zwave_plus_version"),
            "is_controller_node": node.get("is_controller_node"),
        }
        return ok("zwave", "diagnostics", node_status=node_status)

    if action == "network_status":
        entry_id = await _zwave_entry_id(client)
        if not entry_id:
            return integration_not_found("zwave", "zwave_js")
        net = (
            await ws_call(
                client,
                "zwave_js/network_status",
                entry_id=entry_id,
                context={"entry_id": entry_id},
            )
            or {}
        )
        controller = net.get("controller", {}) or {}
        nodes = controller.get("nodes", []) or []
        # Drop the (potentially huge) embedded node list from the controller
        # view; the capped per-node summaries below carry the node detail.
        controller_view = {k: v for k, v in controller.items() if k != "nodes"}
        node_summaries = [
            {
                "node_id": n.get("node_id"),
                "status": n.get("status"),
                "is_routing": n.get("is_routing"),
                "is_secure": n.get("is_secure"),
                "zwave_plus_version": n.get("zwave_plus_version"),
                "is_controller_node": n.get("is_controller_node"),
            }
            for n in nodes[:_NODE_LIMIT]
        ]
        out = ok(
            "zwave",
            "network_status",
            config_entry_id=entry_id,
            controller=controller_view,
            nodes=node_summaries,
            count=len(node_summaries),
            total_count=len(nodes),
        )
        if len(nodes) > _NODE_LIMIT:
            out["truncated"] = True
        return out

    if action == "ping":
        # zwave_js.ping is a plain service (no response support); it targets the
        # node by device_id and returns the list of affected states.
        result = await client.call_service("zwave_js", "ping", {"device_id": device_id})
        return ok("zwave", "ping", result=result)

    if action == "add":
        entry_id = await _zwave_entry_id(client)
        if not entry_id:
            integration_required("zwave", "zwave_js")

        if args.get("smart_start"):
            # SmartStart: add the provisioning info to the controller's list; the
            # node joins automatically (and securely) the next time it is powered.
            require(
                args,
                ActionSpec("", required=("qr_provisioning_information",)),
                "zwave",
                "add",
            )
            result = await ws_call(
                client,
                "zwave_js/provision_smart_start_node",
                entry_id=entry_id,
                qr_provisioning_information=args.get("qr_provisioning_information"),
                device_name=args.get("device_name"),
                area_id=args.get("area_id"),
                context={"entry_id": entry_id},
            )
            return ok(
                "zwave", "add", mode="smart_start", result=result, long_running=True
            )

        # Active inclusion. Non-interactive only: require exactly one pre-shared
        # credential so no S2 read-the-PIN handshake is needed.
        provided = [c for c in _ADD_CREDENTIALS if args.get(c)]
        if not provided:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "zwave/add requires one of "
                    + ", ".join(_ADD_CREDENTIALS)
                    + " (or params.smart_start=True). Interactive S2 "
                    "read-the-PIN pairing is not scriptable — use the Home "
                    "Assistant UI.",
                    context={"radio": "zwave", "action": "add"},
                    suggestions=[
                        "Pass qr_provisioning_information for a scanned device",
                        "Set params.smart_start=True to add to the SmartStart list",
                        "Use the Home Assistant UI for interactive S2 PIN pairing",
                    ],
                )
            )
        if len(provided) > 1:
            # HA's add_node schema marks these as vol.Exclusive — passing more
            # than one is rejected upstream, so fail fast with a clear conflict.
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "zwave/add accepts only one inclusion credential, but "
                    + str(len(provided))
                    + " were provided: "
                    + ", ".join(provided)
                    + ". "
                    + ", ".join(_ADD_CREDENTIALS)
                    + " are mutually exclusive.",
                    context={"radio": "zwave", "action": "add", "conflict": provided},
                    suggestions=[
                        "Pass exactly one of " + ", ".join(_ADD_CREDENTIALS),
                    ],
                )
            )
        result = await ws_call(
            client,
            "zwave_js/add_node",
            entry_id=entry_id,
            inclusion_strategy=args.get("inclusion_strategy"),
            qr_provisioning_information=args.get("qr_provisioning_information"),
            qr_code_string=args.get("qr_code_string"),
            planned_provisioning_entry=args.get("planned_provisioning_entry"),
            dsk=args.get("dsk"),
            context={"entry_id": entry_id},
        )
        return ok("zwave", "add", mode="add_node", result=result, long_running=True)

    if action == "remove_device":
        if args.get("failed"):
            # remove_failed_node targets a specific (already-dead) node.
            require(
                args,
                ActionSpec("", required=("device_id",)),
                "zwave",
                "remove_device",
            )
            result = await ws_call(
                client,
                "zwave_js/remove_failed_node",
                device_id=device_id,
                context={"device_id": device_id},
            )
            return ok("zwave", "remove_device", mode="failed", result=result)

        # Default: open an exclusion window on the controller (long-running; the
        # user triggers exclusion on the physical device to complete it).
        entry_id = await _zwave_entry_id(client)
        if not entry_id:
            integration_required("zwave", "zwave_js")
        result = await ws_call(
            client,
            "zwave_js/remove_node",
            entry_id=entry_id,
            context={"entry_id": entry_id},
        )
        return ok(
            "zwave",
            "remove_device",
            mode="exclusion",
            result=result,
            long_running=True,
        )

    if action == "reinterview":
        result = await ws_call(
            client,
            "zwave_js/refresh_node_info",
            device_id=device_id,
            context={"device_id": device_id},
        )
        return ok("zwave", "reinterview", result=result, long_running=True)

    if action == "rebuild_routes":
        if args.get("scope") == "network":
            entry_id = await _zwave_entry_id(client)
            if not entry_id:
                integration_required("zwave", "zwave_js")
            result = await ws_call(
                client,
                "zwave_js/begin_rebuilding_routes",
                entry_id=entry_id,
                context={"entry_id": entry_id},
            )
            return ok(
                "zwave",
                "rebuild_routes",
                scope="network",
                result=result,
                long_running=True,
            )
        require(
            args,
            ActionSpec("", required=("device_id",)),
            "zwave",
            "rebuild_routes",
        )
        result = await ws_call(
            client,
            "zwave_js/rebuild_node_routes",
            device_id=device_id,
            context={"device_id": device_id},
        )
        return ok(
            "zwave",
            "rebuild_routes",
            scope="node",
            result=result,
            long_running=True,
        )

    if action == "set_config_param":
        result = await ws_call(
            client,
            "zwave_js/set_config_parameter",
            device_id=device_id,
            property=args.get("property"),
            endpoint=args.get("endpoint", 0),
            property_key=args.get("property_key"),
            value=args.get("value"),
            context={"device_id": device_id, "property": args.get("property")},
        )
        return ok("zwave", "set_config_param", result=result)

    if action == "firmware_update":
        update_entity = await resolve_update_entity(
            client, device_id, platform="zwave_js"
        )
        await client.call_service("update", "install", {"entity_id": update_entity})
        return ok(
            "zwave", "firmware_update", entity_id=update_entity, long_running=True
        )

    if action == "hard_reset":
        entry_id = await _zwave_entry_id(client)
        if not entry_id:
            integration_required("zwave", "zwave_js")
        result = await ws_call(
            client,
            "zwave_js/hard_reset_controller",
            entry_id=entry_id,
            context={"entry_id": entry_id},
        )
        return ok("zwave", "hard_reset", result=result, long_running=True)

    # Unreachable: dispatcher validates action against SUPPORTED first.
    raise AssertionError(f"unhandled zwave action: {action}")
