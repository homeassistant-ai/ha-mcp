"""A tag's name and icon in its snapshot and on restore.

Core keeps a tag's name in the entity registry, not in the tag store:
``tag/list`` fills ``name`` with ``entity.name or entity.original_name`` (the
default "Tag <id>"), and ``tag/update`` writes any name it is sent into the
registry. So the snapshot records the registry's own name (``None`` when the
tag was never named), and a restore sends a name only when there was one.
"""

from __future__ import annotations

from typing import Any

from .backup_entity_ids import _manager, registry_row, restore_registry_icon


async def _tag_entity(client: Any, tag_id: str) -> dict[str, Any] | None:
    return await registry_row(client, "tag", tag_id)


async def tag_snapshot(client: Any, item: dict[str, Any]) -> dict[str, Any]:
    """The listed tag with its registry name instead of the listed fallback."""
    entity = await _tag_entity(client, str(item.get("id")))
    return {
        **item,
        "name": entity.get("name") if entity else item.get("name"),
        "registry_icon": entity.get("icon") if entity else None,
    }


async def restore_tag(client: Any, tag_id: str, payload: dict[str, Any]) -> Any:
    """``tag/update`` without a name the tag never had; a name given after the
    snapshot is cleared in the registry, which ``tag/update`` cannot do."""
    bm = _manager()
    recorded = "registry_icon" in payload
    registry_icon = payload.pop("registry_icon", None)
    unnamed = payload.get("name") is None
    if unnamed:
        payload.pop("name", None)
    result = await bm._ws_send(client, payload)
    entity = await _tag_entity(client, tag_id) if unnamed else None
    if entity and entity.get("name") is not None:
        async with bm.registry_update_lock("entity", entity["entity_id"]):
            await bm._ws_send(
                client,
                {
                    "type": "config/entity_registry/update",
                    "entity_id": entity["entity_id"],
                    "name": None,
                },
            )
    if not recorded:
        return result
    warnings = await restore_registry_icon(client, "tag", tag_id, registry_icon)
    return {**result, "warnings": warnings} if warnings else result
