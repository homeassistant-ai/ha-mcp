"""Best-effort radio enrichment for ha_get_device single-device results."""

import logging
from typing import Any

from ..client.rest_client import HomeAssistantAPIError, HomeAssistantConnectionError

logger = logging.getLogger(__name__)


async def enrich_zha_metrics(
    client: Any, device_info: dict[str, Any], warnings: list[str]
) -> None:
    """Fetch ZHA radio metrics (LQI/RSSI) and add to device_info in-place.

    Enrichment is best-effort, but a skipped fetch is named in ``warnings`` so
    a device answered without ``radio_metrics`` is distinguishable from one
    whose radio genuinely reports none (#1947).
    """
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
        else:
            warnings.append(
                f"ZHA radio metrics unavailable: {zha_result.get('error') or 'request failed'}"
            )
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
        warnings.append(f"ZHA radio metrics unavailable: {e}")


async def enrich_zwave_status(
    client: Any, device_id: str, device_info: dict[str, Any], warnings: list[str]
) -> None:
    """Fetch Z-Wave node status and add to device_info in-place.

    Best-effort like the ZHA metrics, and named in ``warnings`` on failure for
    the same reason (#1947).
    """
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
        else:
            warnings.append(
                f"Z-Wave node status unavailable: {zwave_result.get('error') or 'request failed'}"
            )
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
        warnings.append(f"Z-Wave node status unavailable: {e}")


async def enrich_matter_diagnostics(
    client: Any, device_id: str, device_info: dict[str, Any], warnings: list[str]
) -> None:
    """Fetch Matter node diagnostics and add to device_info in-place.

    Mirrors enrich_zwave_status: surfaces the Matter equivalent of Z-Wave node
    status — network type (wifi/thread), reachability, IPs and joined fabrics,
    and names a skipped fetch in ``warnings`` (#1947).
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
        else:
            warnings.append(
                f"Matter node diagnostics unavailable: {result.get('error') or 'request failed'}"
            )
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
        warnings.append(f"Matter node diagnostics unavailable: {e}")
