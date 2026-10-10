"""A zone's registry icon in its snapshot, and a removed zone on restore.

The helper tools keep a zone's icon in the entity registry unless the stored
zone already has one (#2643), so the stored item alone can miss it: the
snapshot records the registry's icon too. Core's ``zone/update`` needs the
stored zone; once it was removed, the restore creates it again, and Core picks
the new zone's id from its name. Core removed the old entity's registry entry
with the zone, so a recreated zone gets a new one: only the recorded icon is put
back, not an area, labels or a renamed entity_id.
"""

from __future__ import annotations

import logging
from typing import Any

from .backup_entity_ids import _manager, registry_row
from .client.rest_client import HomeAssistantCommandError, HomeAssistantError

logger = logging.getLogger(__name__)


async def zone_snapshot(client: Any, item: dict[str, Any]) -> dict[str, Any]:
    """The stored zone with the icon its registry entry sets, if any.

    A registry that cannot be read leaves the key out, so the stored zone is
    still captured and a restore leaves the registry icon alone.
    """
    try:
        entity = await registry_row(client, "zone", str(item.get("id")))
    except HomeAssistantError as err:
        logger.warning("zone snapshot without its registry icon: %s", err)
        return dict(item)
    return {**item, "registry_icon": entity.get("icon") if entity else None}


async def _recreate(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Create the removed zone again, unless an earlier restore already did.

    Core picks a created zone's id from its name, so a zone an earlier restore
    of this snapshot created has a new id and this restore's update fails too.
    A stored zone holding exactly the snapshot's fields is that zone, and it is
    left as it is; any other zone, even one with the same name, is not touched.
    """
    bm = _manager()
    fields = {k: v for k, v in payload.items() if k not in ("type", "zone_id")}
    zones = bm._require_list(
        await bm._ws_send(client, {"type": "zone/list"}), "zone/list"
    )
    restored = next(
        (z for z in zones if all(z.get(k) == v for k, v in fields.items())), None
    )
    if restored is None:
        restored = await bm._ws_send(client, {**fields, "type": "zone/create"})
    return {**restored, "restore_mode": "recreated"}


async def restore_zone(client: Any, zone_id: str, payload: dict[str, Any]) -> Any:
    """``zone/update``, or a recreate when the zone was removed; then the
    registry icon the snapshot recorded. A snapshot without a ``registry_icon``
    key leaves the registry alone; a recorded ``None`` clears it.
    """
    bm = _manager()
    recorded = "registry_icon" in payload
    registry_icon = payload.pop("registry_icon", None)
    try:
        result = await bm._ws_send(client, dict(payload))
    except HomeAssistantCommandError as err:
        if err.code != "not_found":
            raise
        result = await _recreate(client, payload)
        zone_id = result.get("id", zone_id)
    warnings: list[str] = []
    if result.get("icon") != payload.get("icon"):
        warnings.append(
            f"The zone keeps its stored icon {result.get('icon')}: Core's zone "
            "update cannot remove an icon stored after the snapshot."
        )
    if recorded:
        warnings += await _restore_registry_icon(client, zone_id, registry_icon)
    return {**result, "warnings": warnings} if warnings else result


async def _restore_registry_icon(
    client: Any, zone_id: str, registry_icon: str | None
) -> list[str]:
    bm = _manager()
    try:
        entity = await registry_row(client, "zone", zone_id)
        if entity is None:
            if registry_icon is None:
                return []
            return [
                f"The registry icon {registry_icon} was not restored: "
                f"zone {zone_id} has no entity."
            ]
        async with bm.registry_update_lock("entity", entity["entity_id"]):
            await bm._ws_send(
                client,
                {
                    "type": "config/entity_registry/update",
                    "entity_id": entity["entity_id"],
                    "icon": registry_icon,
                },
            )
    except HomeAssistantError as err:
        return [f"The zone was restored, but its registry icon was not: {err}"]
    return []
