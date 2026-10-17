"""The zone a zone tool names, its registry icon, and a removed zone on restore.

The helper tools keep a zone's icon in the entity registry unless the stored
zone already has one (#2643), so the stored item alone can miss it: the
snapshot records the registry's icon too. Core's ``zone/update`` needs the
stored zone; once it was removed, the restore creates it again, and Core takes
the new zone's id from the slug of its name. Core keeps a removed zone's
registry entry for 30 days, so a zone created again under its old id in that
window gets back its entity_id (if still free), registry name, area, labels
and icon; otherwise, or under another id (the zone was renamed since it was
created, or its slug was taken), it gets a new entry. Either way the recorded
registry icon is written on top.
"""

from __future__ import annotations

from typing import Any

from .backup_entity_ids import _manager, restore_registry_icon, with_registry_icon
from .backup_helper_target import stored_target
from .client.rest_client import HomeAssistantCommandError


async def fetch_zone(client: Any, zone_id: str) -> Any:
    """The snapshot of the zone ``ha_set_zone`` or ``ha_remove_zone`` changes,
    resolved by ``stored_target``: a bare id is a storage id, and only
    ``zone.<name>`` is the entity_id of a zone's registry entry, which also
    finds a renamed zone."""
    bm = _manager()
    zones = bm._require_list(
        await bm._ws_send(client, {"type": "zone/list"}), "zone/list"
    )
    stored = {str(zone.get("id")): zone for zone in zones}
    storage_id, _ = stored_target(
        "zone", zone_id, stored, await bm._entity_registry_rows(client)
    )
    zone = stored.get(storage_id) if storage_id else None
    return await with_registry_icon(client, "zone", zone) if zone is not None else None


async def _recreate(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Create the removed zone again, unless an earlier restore already did.

    Core takes a created zone's id from the slug of its name. When that is the
    snapshot's id, a zone an earlier restore created has it, and a repeated
    restore is a plain ``zone/update`` that never gets here. When it is not
    (a zone renamed since it was created, or one created with a suffix), this
    restore's update fails too: a stored zone whose stored fields all equal the
    snapshot's is taken for the zone the earlier restore created and left as
    it is; Core allows identical zones, so ``restore_zone`` names it in a
    warning. Any other zone, even one with the same name, is not touched.
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
    key leaves the registry alone; a recorded ``None`` clears it. A repeated
    restore updates the zone again when the earlier one recreated it under the
    snapshot's id. Under another id (see ``_recreate``) that zone is left as it
    is, registry icon included, since it may be another, identical zone. A
    zone created since the removal can hold the freed id; the update then
    writes it, after the restore's safety backup of it.
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
