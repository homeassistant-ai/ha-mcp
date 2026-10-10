"""Snapshot and restore of the helper types whose icon lives in the registry.

Tag, zone and person keep fields in the entity registry that their stored
item lacks (see ``backup_entity_ids.with_registry_icon``); ``backup_manager``
looks their snapshot and restore up here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .backup_entity_ids import update_with_registry_icon, with_registry_icon
from .backup_tags import restore_tag, tag_snapshot
from .backup_zones import restore_zone, zone_snapshot


async def _person_snapshot(client: Any, item: dict[str, Any]) -> dict[str, Any]:
    return await with_registry_icon(client, "person", item)


async def _restore_person(client: Any, person_id: str, payload: dict[str, Any]) -> Any:
    return await update_with_registry_icon(client, "person", person_id, payload)


REGISTRY_SNAPSHOTS: dict[str, Callable[[Any, dict[str, Any]], Awaitable[Any]]] = {
    "tag": tag_snapshot,
    "zone": zone_snapshot,
    "person": _person_snapshot,
}

REGISTRY_RESTORES: dict[str, Callable[[Any, str, dict[str, Any]], Awaitable[Any]]] = {
    "tag": restore_tag,
    "zone": restore_zone,
    "person": _restore_person,
}
