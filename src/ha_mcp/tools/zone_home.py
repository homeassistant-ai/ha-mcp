"""The home zone's location and radius for ``ha_set_zone``.

Home Assistant keeps them in its general settings, not in the zone storage;
the Zones panel writes them with ``config/core/update``, and Core's zone
integration then moves ``zone.home``. Nothing else of the home zone can be set.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..errors import create_validation_error
from .helpers import raise_core_refusal, raise_tool_error

HOME_ZONE = "zone.home"
HOME_ZONE_FIELDS = ("latitude", "longitude", "radius")
# Core moves zone.home in a listener of its config update, after the reply.
READBACK_TIMEOUT_S = 5.0
READBACK_INTERVAL_S = 0.2


def is_home_zone_id(zone_id: str) -> bool:
    return zone_id in ("home", HOME_ZONE)


async def set_home_zone(
    client: Any, zone_id: str, given: dict[str, Any], *, wait: bool
) -> dict[str, Any]:
    """Write the given location fields with ``config/core/update`` and read
    ``zone.home`` back; any other field is refused before the write."""
    others = sorted(set(given) - set(HOME_ZONE_FIELDS))
    if others:
        raise_tool_error(
            create_validation_error(
                f"The home zone takes only {', '.join(HOME_ZONE_FIELDS)}; "
                f"{', '.join(others)} cannot be set on it",
                context={"zone_id": zone_id, "refused_fields": others},
                suggestions=[
                    "Pass only latitude, longitude and radius for zone.home",
                    (
                        "The home zone's name is the location name under "
                        "Settings > System > General"
                    ),
                ],
            )
        )
    result = await client.send_websocket_message(
        {"type": "config/core/update", **given}
    )
    if not result.get("success"):
        raise_core_refusal(
            "Failed to update the home zone", result, {"zone_id": zone_id}
        )
    state, moved = await _read_home_zone(client, given, wait=wait)
    response: dict[str, Any] = {
        "success": True,
        "zone_data": state.get("attributes") or {},
        "zone_id": "home",
        "entity_id": HOME_ZONE,
        "message": "Successfully updated the home zone",
        "updated_fields": sorted(given),
    }
    if not moved:
        response["warnings"] = [
            "Home Assistant took the new location, but zone.home did not show "
            "it yet when read back"
        ]
    return response


async def _read_home_zone(
    client: Any, given: dict[str, Any], *, wait: bool
) -> tuple[dict[str, Any], bool]:
    """``zone.home``'s state, and whether it shows every given value."""
    deadline = time.monotonic() + (READBACK_TIMEOUT_S if wait else 0)
    while True:
        state = await client.get_entity_state(HOME_ZONE)
        attributes = state.get("attributes") or {}
        if all(attributes.get(key) == value for key, value in given.items()):
            return state, True
        if time.monotonic() >= deadline:
            return state, False
        await asyncio.sleep(READBACK_INTERVAL_S)
