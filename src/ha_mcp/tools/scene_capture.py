"""Capture entities' current states into a scene config, as the scene editor does."""

from __future__ import annotations

from typing import Any

from ..client.exceptions import HomeAssistantAPIError, HomeAssistantError
from ..errors import ErrorCode, create_error_response
from .component_devices import fetch_device_via_component
from .helpers import exception_to_structured_error, raise_tool_error
from .search.state import fetch_states_via_component

# The scene editor leaves these domains out of a picked device's entities
# and of its entity picker: frontend 20261028.6, src/data/scene.ts
# SCENE_IGNORED_DOMAINS, a frontend-only list.
SCENE_IGNORED_DOMAINS = frozenset(
    {
        "binary_sensor", "button", "configuration", "device_tracker", "event",
        "image_processing", "infrared", "input_button", "persistent_notification",
        "person", "radio_frequency", "scene", "schedule", "script", "sensor", "sun",
        "update", "weather", "zone",
    }
)  # fmt: skip


async def _read_states(
    client: Any, entity_ids: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """The entities' states and the ids without one: one component frame, or
    one REST read per id when the component path returns ``None``; a REST
    failure other than 404 raises."""
    component = await fetch_states_via_component(client, entity_ids)
    if component is not None:
        found = component["states"]
        missing = [entity_id for entity_id in entity_ids if entity_id not in found]
        return list(found.values()), missing
    states: list[dict[str, Any]] = []
    missing = []
    for entity_id in entity_ids:
        try:
            states.append(await client.get_entity_state(entity_id))
        except HomeAssistantError as e:
            if not (isinstance(e, HomeAssistantAPIError) and e.status_code == 404):
                exception_to_structured_error(
                    e,
                    context={"action": "capture", "entity_id": entity_id},
                    suggestions=[
                        f"Reading {entity_id}'s state to capture it failed; "
                        "retry, or capture without it"
                    ],
                )
            missing.append(entity_id)
    return states, missing


async def _device_members(client: Any, device_ids: list[str]) -> dict[str, list[str]]:
    """Each device's entities as the scene editor's device picker lists them:
    its registry entries with no entity category, not hidden, outside
    ``SCENE_IGNORED_DOMAINS``. The rows come from the component's device read."""
    members: dict[str, list[str]] = {}
    for device_id in device_ids:
        reply = await fetch_device_via_component(
            client, device_id, include_entities=True
        )
        if reply is None or not isinstance(reply.get("entities"), list):
            raise_tool_error(
                create_error_response(
                    ErrorCode.COMPONENT_NOT_INSTALLED,
                    "capture_devices needs the ha_mcp_tools component's device "
                    "read, which is missing or did not answer; nothing was written",
                    suggestions=[
                        "Install or update the ha_mcp_tools component",
                        (
                            "If it is installed and current, retry; the server log "
                            "names the failure"
                        ),
                        (
                            "Or capture the device's entities with capture_entities; "
                            "the scene editor then lists them as single entities"
                        ),
                    ],
                    context={"device_id": device_id},
                )
            )
        if reply["device"] is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"Device not found: {device_id}; nothing was written",
                    suggestions=["Use ha_search to find the device_id"],
                    context={"device_id": device_id},
                )
            )
        members[device_id] = [
            row["entity_id"]
            for row in reply["entities"]
            if not row.get("entity_category")
            and not row.get("hidden_by")
            and row["entity_id"].split(".")[0] not in SCENE_IGNORED_DOMAINS
        ]
    return members


async def capture_scene_states(
    client: Any,
    config: dict[str, Any],
    entity_ids: list[str],
    device_ids: list[str],
) -> None:
    """Write the current states of ``entity_ids`` and of the devices'
    entities into ``config['entities']``.

    The frontend's scene editor stores a state with all its attributes and
    marks an entity it added on its own ``entity_only`` in ``metadata``; a
    device's entity gets no mark, and one without a state is skipped. A picked
    entity without a state, or a device none of whose entities has one, refuses
    the call. Core applies what each domain can reproduce. A captured state
    replaces the entity's entry; an entity already in the scene keeps its
    metadata. An ``entities`` or ``metadata`` that is no dict is left for Core
    to refuse.
    """
    devices = await _device_members(client, device_ids)
    member_ids = [e for members in devices.values() for e in members]
    states, missing = await _read_states(
        client, list(dict.fromkeys([*entity_ids, *member_ids]))
    )
    # Core finds a differently cased id; the entry is keyed as Core names it.
    captured = {
        state["entity_id"]: {**state.get("attributes", {}), "state": state["state"]}
        for state in states
    }
    missing = [e for e in missing if e in entity_ids]
    empty = [
        d for d, members in devices.items() if not any(m in captured for m in members)
    ]
    if empty:
        raise_tool_error(
            create_error_response(
                ErrorCode.ENTITY_NOT_FOUND,
                f"Device {', '.join(empty)} has no entity with a state the scene "
                "editor saves; nothing was written",
                suggestions=["Check the device's entities with ha_get_device"],
                context={
                    "device_ids": empty,
                    "members": {d: devices[d] for d in empty},
                },
            )
        )
    if missing:
        raise_tool_error(
            create_error_response(
                ErrorCode.ENTITY_NOT_FOUND,
                f"No state to capture for {', '.join(missing)}; nothing was written",
                suggestions=["Use ha_search to find the entity_ids"],
                context={"entity_ids": missing},
            )
        )
    entities = config.setdefault("entities", {})
    metadata = config.setdefault("metadata", {})
    if not isinstance(entities, dict) or not isinstance(metadata, dict):
        return
    picked = {e.lower() for e in entity_ids}
    for entity_id, state in captured.items():
        # A differently cased stored key is this entity too; it keeps its metadata.
        for key in [k for k in entities if k != entity_id and k.lower() == entity_id]:
            entities.setdefault(entity_id, entities.pop(key))
            if key in metadata:
                metadata.setdefault(entity_id, metadata.pop(key))
        if entity_id not in entities and entity_id in picked:
            metadata[entity_id] = {"entity_only": True}
        entities[entity_id] = state
