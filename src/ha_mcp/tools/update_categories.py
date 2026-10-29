"""The category ``ha_manage_updates`` files an update entity under."""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Integrations whose update entities are store repositories; Home Assistant
# 2026.11 moved them from the HACS integration to the built-in Marketplace,
# whose entities carry no "hacs" in their entity_id.
_STORE_PLATFORMS = frozenset({"hacs", "marketplace"})


async def update_platforms(client: Any) -> dict[str, str]:
    """Map each registered update entity to the integration that owns it."""
    reply = await client.send_websocket_message(
        {"type": "config/entity_registry/list_for_display"}
    )
    if not reply.get("success"):
        logger.debug("Entity registry unavailable for update categories: %s", reply)
        return {}
    return {
        row["ei"]: row.get("pl", "")
        for row in (reply.get("result") or {}).get("entities", [])
        if row.get("ei", "").startswith("update.")
    }


def categorize_update(
    entity_id: str, attributes: dict[str, Any], platform: str = ""
) -> str:
    """Categorize an update entity by its integration, entity_id and attributes."""
    if platform in _STORE_PLATFORMS:
        return "hacs"

    entity_lower = entity_id.lower()
    # Use 'or ""' to handle both missing keys AND explicit None values
    title_lower = (attributes.get("title") or "").lower()

    if "home_assistant_core" in entity_lower or (
        "core" in entity_lower and "home_assistant" in title_lower
    ):
        return "core"

    if "operating_system" in entity_lower or "haos" in entity_lower:
        return "os"

    if "supervisor" in entity_lower:
        return "supervisor"

    # Add-ons (typically named update.xxx_update where xxx is addon name)
    if "add-on" in title_lower or "addon" in title_lower:
        return "addons"

    device_patterns = ["esphome", "zwave", "zigbee", "zha", "matter", "firmware"]
    if any(
        pattern in entity_lower or pattern in title_lower for pattern in device_patterns
    ):
        return "devices"

    return "other"
