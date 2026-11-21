"""Capture entities' current states into a scene config, as the scene editor does."""

from __future__ import annotations

from typing import Any, NoReturn

from ..client.exceptions import HomeAssistantAPIError, HomeAssistantError
from ..errors import ErrorCode, create_error_response
from .component_api import ask_component
from .component_devices import fetch_device_via_component
from .helpers import exception_to_structured_error, raise_tool_error
from .search.state import fetch_states_via_component


async def _read_states(client: Any, entity_ids: list[str]) -> list[dict[str, Any]]:
    """The states Core answers for ``entity_ids``: one component frame, or one
    REST read per id when the component path returns ``None``; a REST 404 is
    skipped, any other REST failure raises."""
    if not entity_ids:
        return []
    component = await fetch_states_via_component(client, entity_ids)
    if component is not None:
        return list(component["states"].values())
    states: list[dict[str, Any]] = []
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
    return states


def _refuse_without_component(read: str, context: dict[str, Any]) -> NoReturn:
    """capture_devices without the component's ``read``: nothing is written."""
    raise_tool_error(
        create_error_response(
            ErrorCode.COMPONENT_NOT_INSTALLED,
            f"capture_devices needs the ha_mcp_tools component's {read}, which "
            "it does not serve or did not answer; nothing was written",
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
            context=context,
        )
    )


async def _device_members(
    client: Any, device_ids: list[str], problems: list[dict[str, Any]]
) -> dict[str, list[str]]:
    """Each device's entities as the scene editor's device picker lists them:
    its registry entries with no entity category, not hidden, outside the
    domains the editor ignores, which the component reads from the installed
    frontend. The rows come from the component's device read; an unknown
    device is added to ``problems``."""
    members: dict[str, list[str]] = {}
    if not device_ids:
        return members
    editor = await ask_component(client, "scene_ignored_domains")
    if editor is None:
        _refuse_without_component(
            "read of the domains the scene editor ignores",
            {"device_ids": device_ids},
        )
    ignored = editor["domains"]
    for device_id in device_ids:
        reply = await fetch_device_via_component(
            client, device_id, include_entities=True
        )
        if reply is None or not isinstance(reply.get("entities"), list):
            _refuse_without_component("device read", {"device_id": device_id})
        if reply["device"] is None:
            problems.append(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"Device not found: {device_id}; nothing was written",
                    suggestions=["Use ha_search to find the device_id"],
                    context={"device_id": device_id},
                )
            )
            continue
        members[device_id] = [
            row["entity_id"]
            for row in reply["entities"]
            if not row.get("entity_category")
            and not row.get("hidden_by")
            and row["entity_id"].split(".")[0] not in ignored
        ]
    return members


async def read_captures(
    client: Any,
    entity_ids: list[str],
    device_ids: list[str],
    problems: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """The current states of ``entity_ids`` and of the devices' entities, keyed
    by Core's entity_id, as the scene editor stores them: a state with all its
    attributes. A device entity without a state is skipped. An unknown device,
    a device none of whose entities has a state, and a picked entity without
    one are added to ``problems``."""
    devices = await _device_members(client, device_ids, problems)
    member_ids = [e for members in devices.values() for e in members]
    states = await _read_states(client, list(dict.fromkeys([*entity_ids, *member_ids])))
    # Core finds a differently cased id; the entry is keyed as Core names it.
    captured = {
        state["entity_id"]: {**state.get("attributes", {}), "state": state["state"]}
        for state in states
    }
    # Judged by the reply's entity_id: a REST read of ``a/../b`` answers ``b``.
    missing = [e for e in entity_ids if e.lower() not in captured]
    empty = [
        d for d, members in devices.items() if not any(m in captured for m in members)
    ]
    if empty:
        problems.append(
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
        problems.append(
            create_error_response(
                ErrorCode.ENTITY_NOT_FOUND,
                f"No state to capture for {', '.join(missing)}; nothing was written",
                suggestions=["Use ha_search to find the entity_ids"],
                context={"entity_ids": missing},
            )
        )
    return captured


def merge_captures(
    config: dict[str, Any], captured: dict[str, dict[str, Any]], entity_ids: list[str]
) -> None:
    """Write ``captured`` into ``config['entities']`` as the scene editor saves:
    an entity picked on its own (in ``entity_ids``) that is new to the scene is
    marked ``entity_only`` in ``metadata``, a device's entity is not. A
    captured state replaces the entity's entry; an entity already in the scene
    keeps its metadata. An ``entities`` or ``metadata`` that is no dict is left
    for Core to refuse."""
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
