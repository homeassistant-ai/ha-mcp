"""A group's snapshot from the state API, restored with the group.set service."""

from __future__ import annotations

from typing import Any

from .backup_entity_ids import _manager


async def fetch_group(client: Any, entity_id: str) -> Any:
    eid = entity_id if entity_id.startswith("group.") else f"group.{entity_id}"
    state = await _manager()._rest_get_or_none(client, f"states/{eid}")
    if state is None:
        return None
    attrs = state.get("attributes", {}) if isinstance(state, dict) else {}
    return {
        "object_id": eid.split(".", 1)[1],
        "name": attrs.get("friendly_name"),
        "entities": attrs.get("entity_id", []),
        "icon": attrs.get("icon"),
    }


async def restore_group(client: Any, entity_id: str, config: Any) -> Any:
    object_id = config.get("object_id") or entity_id.split(".", 1)[-1]
    service_data: dict[str, Any] = {"object_id": object_id}
    for key in ("name", "icon"):
        if config.get(key):
            service_data[key] = config[key]
    if config.get("entities") is not None:
        service_data["entities"] = config["entities"]
    group_set = {"type": "call_service", "domain": "group", "service": "set"}
    return await _manager()._ws_send(
        client, {**group_set, "service_data": service_data}
    )
