"""Matter radio handler for ``ha_manage_radio``.

Wraps the Home Assistant ``matter/*`` WebSocket API: per-node diagnostics and
ping (read), commissioning / multi-admin share-out / fabric removal / interview
(write), and credential provisioning. All commands are single request/response
or fire-and-return; Matter has no interactive PIN handshake (unlike Z-Wave S2).
"""

from __future__ import annotations

from typing import Any

from ...errors import ErrorCode, create_error_response
from ..helpers import raise_tool_error
from .base import ActionSpec, integration_not_found, ok, resolve_entry_id, ws_call

SUPPORTED: dict[str, ActionSpec] = {
    "diagnostics": ActionSpec(
        "Per-node Matter diagnostics: network type (wifi/thread), availability, "
        "IPs, node type, active fabrics.",
        required=("device_id",),
    ),
    "network_status": ActionSpec("Matter fabric / integration summary."),
    "ping": ActionSpec(
        "Actively ping a Matter node at its known IP addresses.",
        required=("device_id",),
    ),
    "commission": ActionSpec(
        "Commission a Matter device from a setup code (QR or manual pairing code).",
        long_running=True,
        required=("code",),
    ),
    "commission_on_network": ActionSpec(
        "Commission an already-networked Matter device by its PIN.",
        long_running=True,
        required=("pin",),
    ),
    "share_out": ActionSpec(
        "Open a commissioning window and return a pairing code so another "
        "controller can add this device (multi-admin).",
        required=("device_id",),
    ),
    "interview": ActionSpec(
        "Re-interview a Matter node to refresh its endpoints/clusters.",
        long_running=True,
        required=("device_id",),
    ),
    "remove_fabric": ActionSpec(
        "Remove ANOTHER controller's fabric from a Matter node (pass "
        "params.fabric_index; see active_fabrics in diagnostics). To detach the "
        "device from Home Assistant itself, delete it with ha_remove_device.",
        destructive=True,
        required=("device_id",),
    ),
    "set_thread": ActionSpec(
        "Provide Thread operational credentials used for the next commissioning.",
        required=("thread_operation_dataset",),
    ),
    "set_wifi_credentials": ActionSpec(
        "Provide WiFi credentials used for the next commissioning.",
        required=("network_name", "password"),
    ),
    "firmware_update": ActionSpec(
        "Install an available OTA firmware update for this Matter node.",
        long_running=True,
        required=("device_id",),
    ),
}


async def _update_entity_for_device(client: Any, device_id: Any) -> str:
    """Return the device's Matter firmware ``update.*`` entity_id."""
    entities = await ws_call(client, "config/entity_registry/list")
    candidates = [
        e
        for e in (entities or [])
        if e.get("device_id") == device_id
        and str(e.get("entity_id", "")).startswith("update.")
    ]
    for e in candidates:
        if e.get("platform") == "matter":
            return str(e["entity_id"])
    if candidates:
        return str(candidates[0]["entity_id"])
    raise_tool_error(
        create_error_response(
            ErrorCode.ENTITY_NOT_FOUND,
            f"No firmware update entity found for device {device_id}",
            context={"device_id": device_id},
            suggestions=["The node may not expose an OTA firmware update entity"],
        )
    )
    raise AssertionError  # unreachable: raise_tool_error always raises


async def handle(client: Any, action: str, args: dict[str, Any]) -> dict[str, Any]:
    """Execute one Matter action (validation/confirm already applied by caller)."""
    device_id = args.get("device_id")

    if action == "diagnostics":
        diag = await ws_call(
            client,
            "matter/node_diagnostics",
            device_id=device_id,
            context={"device_id": device_id},
        )
        # Upstream NodeDiagnostics misspells the IP field "ip_adresses" (one d);
        # normalize so callers see the same "ip_addresses" key ha_get_device
        # surfaces. Copy first so the upstream/result dict is not mutated.
        if isinstance(diag, dict) and "ip_adresses" in diag:
            diag = dict(diag)
            diag["ip_addresses"] = diag.pop("ip_adresses")
        return ok("matter", "diagnostics", diagnostics=diag)

    if action == "ping":
        reachability = await ws_call(
            client,
            "matter/ping_node",
            device_id=device_id,
            context={"device_id": device_id},
        )
        return ok("matter", "ping", reachability=reachability)

    if action == "network_status":
        entry_id = await resolve_entry_id(client, "matter")
        if not entry_id:
            return integration_not_found("matter", "matter")
        return ok(
            "matter",
            "network_status",
            config_entry_id=entry_id,
            note=(
                "Matter exposes health per node; call action='diagnostics' with a "
                "device_id for node-level network type, availability and fabrics."
            ),
        )

    if action == "commission":
        result = await ws_call(
            client,
            "matter/commission",
            code=args.get("code"),
            network_only=bool(args.get("network_only", False)),
        )
        return ok("matter", "commission", result=result, long_running=True)

    if action == "commission_on_network":
        result = await ws_call(
            client,
            "matter/commission_on_network",
            pin=args.get("pin"),
            ip_addr=args.get("ip_addr"),
        )
        return ok("matter", "commission_on_network", result=result, long_running=True)

    if action == "share_out":
        commissioning = await ws_call(
            client,
            "matter/open_commissioning_window",
            device_id=device_id,
            context={"device_id": device_id},
        )
        # Returns setup_pin_code, setup_manual_code, setup_qr_code.
        return ok("matter", "share_out", commissioning=commissioning)

    if action == "interview":
        result = await ws_call(
            client,
            "matter/interview_node",
            device_id=device_id,
            context={"device_id": device_id},
        )
        return ok("matter", "interview", result=result, long_running=True)

    if action == "remove_fabric":
        diag = await ws_call(
            client,
            "matter/node_diagnostics",
            device_id=device_id,
            context={"device_id": device_id},
        )
        active = (diag or {}).get("active_fabric_index")
        fabric_index = args.get("fabric_index")
        # remove_fabric removes ANOTHER controller's fabric. Removing HA's OWN
        # active fabric over that fabric makes the node drop the session, so the
        # Matter server aborts the confirmation (the removal still happens, but
        # reports as an error). The clean "detach from HA" path is deleting the
        # device, so refuse the own-fabric case with that guidance.
        if fabric_index is None or fabric_index == active:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "remove_fabric removes ANOTHER controller's fabric from a "
                    "node; pass params.fabric_index (see active_fabrics). To "
                    "detach this device from Home Assistant itself, delete the "
                    "device with ha_remove_device — removing HA's own active "
                    "fabric here is aborted by the Matter server.",
                    context={
                        "device_id": device_id,
                        "active_fabric_index": active,
                        "active_fabrics": (diag or {}).get("active_fabrics", []),
                    },
                    suggestions=[
                        "Pass params.fabric_index = the other controller's index",
                        "To remove Home Assistant, use ha_remove_device",
                    ],
                )
            )
        result = await ws_call(
            client,
            "matter/remove_matter_fabric",
            device_id=device_id,
            fabric_index=fabric_index,
            context={"device_id": device_id, "fabric_index": fabric_index},
        )
        return ok("matter", "remove_fabric", result=result, fabric_index=fabric_index)

    if action == "set_thread":
        result = await ws_call(
            client,
            "matter/set_thread",
            thread_operation_dataset=args.get("thread_operation_dataset"),
        )
        return ok("matter", "set_thread", result=result)

    if action == "set_wifi_credentials":
        result = await ws_call(
            client,
            "matter/set_wifi_credentials",
            network_name=args.get("network_name"),
            password=args.get("password"),
        )
        return ok("matter", "set_wifi_credentials", result=result)

    if action == "firmware_update":
        entity_id = await _update_entity_for_device(client, device_id)
        await client.call_service("update", "install", {"entity_id": entity_id})
        return ok("matter", "firmware_update", entity_id=entity_id, long_running=True)

    # Unreachable: dispatcher validates action against SUPPORTED first.
    raise AssertionError(f"unhandled matter action: {action}")
