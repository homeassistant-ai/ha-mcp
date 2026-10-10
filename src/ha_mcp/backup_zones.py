"""A zone's registry icon in its snapshot, and a removed zone on restore.

The helper tools keep a zone's icon in the entity registry unless the stored
zone already has one (#2643), so the stored item alone can miss it: the
snapshot records the registry's icon too. Core's ``zone/update`` needs the
stored zone; once it was removed, the restore creates it again, and Core picks
the new zone's id from its name.
"""

from __future__ import annotations

from typing import Any

from .backup_entity_ids import _manager
from .client.rest_client import HomeAssistantCommandError


async def _zone_entity(client: Any, zone_id: str) -> dict[str, Any] | None:
    bm = _manager()
    return next(
        (
            row
            for row in await bm._entity_registry_rows(client)
            if row.get("platform") == "zone" and row.get("unique_id") == zone_id
        ),
        None,
    )


async def zone_snapshot(client: Any, item: dict[str, Any]) -> dict[str, Any]:
    """The stored zone with the icon its registry entry sets, if any."""
    entity = await _zone_entity(client, str(item.get("id")))
    return {**item, "registry_icon": entity.get("icon") if entity else None}


async def restore_zone(client: Any, zone_id: str, payload: dict[str, Any]) -> Any:
    """``zone/update``, or ``zone/create`` when the zone was removed; then the
    registry icon the snapshot recorded (snapshots taken before it lack one)."""
    bm = _manager()
    recorded = "registry_icon" in payload
    registry_icon = payload.pop("registry_icon", None)
    try:
        result = await bm._ws_send(client, dict(payload))
    except HomeAssistantCommandError as err:
        if err.code != "not_found":
            raise
        fields = {k: v for k, v in payload.items() if k not in ("type", "zone_id")}
        created = await bm._ws_send(client, {**fields, "type": "zone/create"})
        result = {**created, "restore_mode": "recreated"}
        zone_id = created.get("id", zone_id)
    entity = await _zone_entity(client, zone_id) if recorded else None
    if entity:
        async with bm.registry_update_lock("entity", entity["entity_id"]):
            await bm._ws_send(
                client,
                {
                    "type": "config/entity_registry/update",
                    "entity_id": entity["entity_id"],
                    "icon": registry_icon,
                },
            )
    return result
