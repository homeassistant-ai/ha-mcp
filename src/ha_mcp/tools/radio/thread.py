"""Thread / OpenThread Border Router (OTBR) handler for ``ha_manage_radio``.

Thread is network-scoped, not per-device: operations target the Thread network
and its border router rather than an individual node. Border-router writes use
the ``otbr/*`` WebSocket API and take an OTBR ``extended_address`` — resolved
from ``otbr/info`` when the caller omits it, which covers the common
single-OTBR setup. Dataset listing and router discovery use the
integration-wide ``thread/*`` API. When no OTBR is configured the handler
degrades to an ``available: False`` payload instead of erroring.
"""

from __future__ import annotations

from typing import Any

from .base import ActionSpec, integration_not_found, ok, resolve_entry_id, ws_call

SUPPORTED: dict[str, ActionSpec] = {
    "network_status": ActionSpec(
        "OTBR border-router status keyed by extended address (channel, active "
        "dataset TLVs, extended PAN id, border agent id)."
    ),
    "list_datasets": ActionSpec(
        "List stored Thread operational datasets (the preferred network + others)."
    ),
    "discover_routers": ActionSpec(
        "Start mDNS discovery of Thread border routers. Results arrive as an "
        "out-of-band event stream; this kicks off the scan.",
        long_running=True,
    ),
    "create_network": ActionSpec(
        "Form a brand-new Thread network on the OTBR. Factory-resets the border "
        "router and replaces its current dataset. Targets the only OTBR when "
        "extended_address is omitted.",
        destructive=True,
    ),
    "set_network": ActionSpec(
        "Apply a stored dataset as the OTBR's active Thread network. Replaces "
        "the current network, which can drop existing Thread devices.",
        destructive=True,
        required=("dataset_id",),
    ),
    "set_channel": ActionSpec(
        "Migrate the OTBR's Thread network to a different radio channel.",
        destructive=True,
        required=("channel",),
    ),
    "add_dataset": ActionSpec(
        "Import a Thread operational dataset (hex TLV) from a named source.",
        required=("source", "tlv"),
    ),
}


async def _resolve_extended_address(client: Any, args: dict[str, Any]) -> str | None:
    """Return the target OTBR extended address.

    Uses the caller-supplied value, else the first (typically only) border
    router reported by ``otbr/info``. Returns None when no OTBR is present.
    """
    addr = args.get("extended_address")
    if addr:
        return str(addr)
    info = await ws_call(client, "otbr/info")
    return next(iter(info or {}), None)


async def handle(client: Any, action: str, args: dict[str, Any]) -> dict[str, Any]:
    """Execute one Thread/OTBR action (validation/confirm applied by caller)."""
    if action == "network_status":
        entry_id = await resolve_entry_id(client, "otbr")
        if not entry_id:
            return integration_not_found("thread", "otbr")
        info = await ws_call(client, "otbr/info")
        return ok(
            "thread",
            "network_status",
            config_entry_id=entry_id,
            border_routers=info,
        )

    if action == "list_datasets":
        datasets = await ws_call(client, "thread/list_datasets")
        return ok("thread", "list_datasets", datasets=datasets)

    if action == "discover_routers":
        # Subscription command: it streams discovered routers as events. Kick
        # off the scan and report it started; consumers watch the event stream.
        await ws_call(client, "thread/discover_routers")
        return ok(
            "thread",
            "discover_routers",
            long_running=True,
            note=(
                "Router discovery started; results are delivered as an event "
                "stream, not in this response."
            ),
        )

    if action == "add_dataset":
        result = await ws_call(
            client,
            "thread/add_dataset_tlv",
            source=args.get("source"),
            tlv=args.get("tlv"),
        )
        return ok("thread", "add_dataset", result=result)

    # Remaining actions target a specific OTBR border router.
    extended_address = await _resolve_extended_address(client, args)
    if not extended_address:
        return integration_not_found("thread", "otbr")
    ctx = {"extended_address": extended_address}

    if action == "create_network":
        result = await ws_call(
            client,
            "otbr/create_network",
            extended_address=extended_address,
            context=ctx,
        )
        return ok(
            "thread", "create_network", result=result, extended_address=extended_address
        )

    if action == "set_network":
        result = await ws_call(
            client,
            "otbr/set_network",
            extended_address=extended_address,
            dataset_id=args.get("dataset_id"),
            context=ctx,
        )
        return ok(
            "thread", "set_network", result=result, extended_address=extended_address
        )

    if action == "set_channel":
        result = await ws_call(
            client,
            "otbr/set_channel",
            extended_address=extended_address,
            channel=args.get("channel"),
            context=ctx,
        )
        return ok(
            "thread", "set_channel", result=result, extended_address=extended_address
        )

    # Unreachable: dispatcher validates action against SUPPORTED first.
    raise AssertionError(f"unhandled thread action: {action}")
