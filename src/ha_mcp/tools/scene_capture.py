"""Capture entities' current states into a scene config, as the scene editor does."""

from __future__ import annotations

from typing import Any

from ..client.rest_client import HomeAssistantAPIError
from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, raise_tool_error


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
    captured: dict[str, Any] = {}
    missing: list[str] = []
    for entity_id in entity_ids:
        try:
            state = await client.get_entity_state(entity_id)
        except HomeAssistantAPIError as e:
            if e.status_code != 404:
                exception_to_structured_error(
                    e,
                    context={"action": "capture", "entity_id": entity_id},
                    suggestions=[
                        f"Reading {entity_id}'s state to capture it failed; "
                        "retry, or leave it out of capture_entities"
                    ],
                )
            missing.append(entity_id)
            continue
        captured[entity_id] = {**state.get("attributes", {}), "state": state["state"]}
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
        if entity_id not in entities:
            metadata[entity_id] = {"entity_only": True}
        entities[entity_id] = state
