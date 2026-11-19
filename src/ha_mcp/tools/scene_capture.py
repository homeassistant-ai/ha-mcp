"""Capture entities' current states into a scene config, as the scene editor does."""

from __future__ import annotations

from typing import Any

from ..client.exceptions import HomeAssistantAPIError, HomeAssistantError
from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, raise_tool_error
from .search.state import fetch_states_via_component


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
                        "retry, or leave it out of capture_entities"
                    ],
                )
            missing.append(entity_id)
    return states, missing


async def capture_scene_states(
    client: Any, config: dict[str, Any], entity_ids: list[str]
) -> None:
    """Write each entity's current state into ``config['entities']``.

    The frontend's scene editor stores a state with all its attributes and
    marks an entity it added on its own ``entity_only`` in ``metadata``; Core
    applies what each domain can reproduce. A captured state replaces the
    entity's entry; an entity already in the scene keeps its metadata. An
    ``entities`` or ``metadata`` that is no dict is left for Core to refuse.
    """
    states, missing = await _read_states(client, entity_ids)
    # Core finds a differently cased id; the entry is keyed as Core names it.
    captured = {
        state["entity_id"]: {**state.get("attributes", {}), "state": state["state"]}
        for state in states
    }
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
    for entity_id, state in captured.items():
        # A differently cased stored key is this entity too; it keeps its metadata.
        for key in [k for k in entities if k != entity_id and k.lower() == entity_id]:
            entities.setdefault(entity_id, entities.pop(key))
            if key in metadata:
                metadata.setdefault(entity_id, metadata.pop(key))
        if entity_id not in entities:
            metadata[entity_id] = {"entity_only": True}
        entities[entity_id] = state
