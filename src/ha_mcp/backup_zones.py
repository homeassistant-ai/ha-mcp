"""The zone a zone tool names, its registry icon, and a removed zone on restore.

The helper tools keep a zone's icon in the entity registry unless the stored
zone already has one (#2643), so the stored item alone can miss it: the
snapshot records the registry's icon too. Core's ``zone/update`` needs the
stored zone; once it was removed, the restore creates it again, and Core picks
the new zone's id from its name. Core removed the old entity's registry entry
with the zone, so a recreated zone gets a new one: only the recorded icon is put
back, not an area, labels or a renamed entity_id.
"""

from __future__ import annotations

from typing import Any

from .backup_entity_ids import _manager, restore_registry_icon, with_registry_icon
from .client.rest_client import HomeAssistantCommandError


async def zone_snapshot(client: Any, item: dict[str, Any]) -> dict[str, Any]:
    """The stored zone with the icon its registry entry sets, if any."""
    return await with_registry_icon(client, "zone", item)


async def fetch_zone(client: Any, zone_id: str) -> Any:
    """The snapshot of the zone ``ha_set_zone`` or ``ha_remove_zone`` changes.

    Resolved as ``ZoneTools._resolve_zone`` resolves it: a storage id first,
    else the zone whose registry entry has ``zone_id`` (with or without
    ``zone.``) as its entity_id, which also finds a renamed zone.
    """
    bm = _manager()
    zones = bm._require_list(
        await bm._ws_send(client, {"type": "zone/list"}), "zone/list"
    )
    stored = {zone.get("id"): zone for zone in zones}
    storage_id: str | None = zone_id
    if zone_id not in stored:
        entity_id = zone_id if zone_id.startswith("zone.") else f"zone.{zone_id}"
        storage_id = next(
            (
                row.get("unique_id")
                for row in await bm._entity_registry_rows(client)
                if row.get("platform") == "zone" and row.get("entity_id") == entity_id
            ),
            None,
        )
    zone = stored.get(storage_id)
    return await zone_snapshot(client, zone) if zone is not None else None


async def _recreate(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Create the removed zone again, unless an earlier restore already did.

    Core picks a created zone's id from its name, so a zone an earlier restore
    of this snapshot created has a new id and this restore's update fails too.
    A stored zone whose stored fields all equal the snapshot's is taken for
    that zone and left as it is; Core allows identical zones, so
    ``restore_zone`` names it in a warning. Any other zone, even one with the
    same name, is not touched.
    """
    bm = _manager()
    fields = {k: v for k, v in payload.items() if k not in ("type", "zone_id")}
    zones = bm._require_list(
        await bm._ws_send(client, {"type": "zone/list"}), "zone/list"
    )
    restored = next((z for z in zones if bm._strip_readonly(z, "id") == fields), None)
    if restored is not None:
        return {**restored, "restore_mode": "already_recreated"}
    created = await bm._ws_send(client, {**fields, "type": "zone/create"})
    return {**created, "restore_mode": "recreated"}


async def restore_zone(client: Any, zone_id: str, payload: dict[str, Any]) -> Any:
    """``zone/update``, or a recreate when the zone was removed; then the
    registry icon the snapshot recorded. A snapshot without a ``registry_icon``
    key leaves the registry alone; a recorded ``None`` clears it. A zone an
    earlier restore of the snapshot created is left as it is, registry icon
    included, since it may be another, identical zone.
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
    if result.get("restore_mode") == "already_recreated":
        warning = (
            f"Zone {zone_id} matches every stored field of the snapshot, so it was "
            "taken for the zone an earlier restore of this snapshot created and "
            "left unchanged, its registry icon included. Core allows identical "
            f"zones: if {zone_id} is a different zone, the removed zone was not "
            "created again."
        )
        if registry_icon is not None:
            warning += (
                f" The snapshot's registry icon is {registry_icon}; set it with "
                "ha_set_entity if the earlier restore could not."
            )
        return {**result, "warnings": [warning]}
    warnings: list[str] = []
    if result.get("icon") != payload.get("icon"):
        warnings.append(
            f"The zone keeps its stored icon {result.get('icon')}: Core's zone "
            "update cannot remove an icon stored after the snapshot."
        )
    if recorded:
        warnings += await restore_registry_icon(client, "zone", zone_id, registry_icon)
    return {**result, "warnings": warnings} if warnings else result
