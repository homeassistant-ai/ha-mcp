"""Voice-assistant exposure reads for ``ha_get_entity``.

Core keeps exposure in two places: an entity with a registry entry stores it in
the entry's ``options`` (which ``ha_get_entity`` already returns), and an entity
without one in Core's separate exposed-entities store. This module serves the
listing of exposed entities and the rows for entities without a registry entry.
Writes go through ``ha_set_entity`` (``expose_to``).
"""

import asyncio
import logging
from collections import Counter
from typing import Any

from ..client.rest_client import (
    _NO_ANSWER_ERRORS,
    HomeAssistantCommandError,
    HomeAssistantError,
)
from ..client.websocket_client import get_websocket_client
from .component_api import (
    DEVICE_REGISTRY_CHILD_SEMANTICS,
    ComponentDiscoveryError,
    component_supports,
    component_unasked_warning,
    component_unusable_warning,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .entity_registry_reads import _NOT_IN_REGISTRY, _extract_ws_error, get_entries_raw
from .helpers import (
    core_error_message,
    get_entity_state_or_none,
    raise_core_refusal,
)

logger = logging.getLogger(__name__)

# The ha_mcp_tools/exposure WS command: Core's expose_entity/list map PLUS a
# sibling entity_info enrichment (names/areas). Named once so the routing helper
# and its tests agree on the wire string.
WS_EXPOSURE = "ha_mcp_tools/exposure"
# State reads one bulk lookup keeps in flight, so a long list of ids without
# a registry entry does not take every connection of the client's pool.
STATE_READS_AT_ONCE = 10
_WITHOUT_ENTITY_INFO = "the listing comes without entity_info (names and areas)"


async def _fetch_exposure_via_component(client: Any) -> dict[str, Any] | None:
    """One ``ha_mcp_tools/exposure`` list read; ``None`` ⇒ read Core's list.

    Returns the component payload — ``{exposed_entities: {id: {assistant:
    True}}, entity_info: {id: {...}}}`` — where ``exposed_entities`` holds the
    same entries as Core's ``homeassistant/expose_entity/list`` map. ``None``
    when the component does not advertise the command, or no longer knows it
    (``unknown_command`` → invalidate the cached caps), or when the command
    fails, gets no answer, loses its connection or answers with a malformed
    result: the caller reads Core's list in each case.
    """
    caps = await get_component_caps(client)
    if not (
        component_supports(caps, "exposure")
        and component_supports(caps, DEVICE_REGISTRY_CHILD_SEMANTICS)
    ):
        return None
    try:
        ws = await get_websocket_client(
            url=client.base_url,
            token=client.token,
            verify_ssl=getattr(client, "verify_ssl", None),
        )
        raw = await ws.send_command(WS_EXPOSURE)
    except (HomeAssistantCommandError, *_NO_ANSWER_ERRORS) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning("%s failed; read Core's list: %r", WS_EXPOSURE, exc)
        return None
    result = raw.get("result")
    if not (
        isinstance(result, dict)
        and isinstance(result.get("exposed_entities"), dict)
        and isinstance(result.get("entity_info"), dict)
    ):
        return None
    return result


async def read_exposure(
    client: Any,
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    """Core's exposed-entity map, each entity's names when the component serves
    them, and a warning when it does not.

    Prefers the component's in-process read when advertised: the same map as
    ``expose_entity/list`` plus an ``entity_info`` enrichment (names/areas), so
    a caller needs no second ha_search to name an exposed entity. Otherwise
    reads Core's list, with no ``entity_info``, and the warning says why: no
    component answered (install it), it is too old for the command (update
    it), or the probe could not be asked.
    """
    # Strict, so a refused or failed probe is not taken for no component.
    try:
        caps = await get_component_caps(client, strict=True)
    except ComponentDiscoveryError as exc:
        warning = component_unasked_warning(exc, _WITHOUT_ENTITY_INFO)
    else:
        component = await _fetch_exposure_via_component(client)
        if component is not None:
            return component["exposed_entities"], component["entity_info"], None
        warning = component_unusable_warning(caps, _WITHOUT_ENTITY_INFO)
    result = await client.send_websocket_message(
        {"type": "homeassistant/expose_entity/list"}
    )
    if not result.get("success"):
        raise_core_refusal("Failed to get exposure settings", result)
    exposed = (result.get("result") or {}).get("exposed_entities") or {}
    return exposed, {}, warning


def list_exposures(
    exposed_entities: dict[str, Any],
    entity_info: dict[str, Any],
    assistant: str | None,
) -> dict[str, Any]:
    """The exposed entities, all or those exposed to one assistant."""
    filtered = exposed_entities
    if assistant is not None:
        filtered = {
            eid: settings
            for eid, settings in exposed_entities.items()
            if settings.get(assistant)
        }
    response: dict[str, Any] = {
        "success": True,
        "exposed_entities": filtered,
        "count": len(filtered),
    }
    if assistant is None:
        response["summary"] = dict(
            Counter(asst for settings in filtered.values() for asst in settings)
        )
    if entity_info:
        response["entity_info"] = {
            eid: entity_info[eid] for eid in filtered if eid in entity_info
        }
    return response


async def unregistered_lookup(
    client: Any, entity_ids: list[str]
) -> dict[str, tuple[str, dict[str, Any]]]:
    """Each id without a registry entry that Home Assistant knows, with its key
    in Core's exposed-entities store and its ``exposed_to`` map.

    Core reads the exposure of an entity with a state under the state's id;
    an id without a state is known when Core's ``expose_entity/list`` lists it
    as sent, which it does while some assistant has it stored as exposed. Any
    other id is left out, so the caller reports it as not found. Only the map
    is needed, so Core's list is read directly rather than the component's.

    Raises HomeAssistantError when the store or a state cannot be read, since
    whether Home Assistant knows the ids is then still open.
    """
    if not entity_ids:
        return {}
    in_flight = asyncio.Semaphore(STATE_READS_AT_ONCE)

    async def bounded_state_entity_id(entity_id: str) -> str | None:
        async with in_flight:
            return await _state_entity_id(client, entity_id)

    reply, *state_ids = await asyncio.gather(
        client.send_websocket_message({"type": "homeassistant/expose_entity/list"}),
        *(bounded_state_entity_id(eid) for eid in entity_ids),
    )
    if not reply.get("success"):
        # The "Command failed:" prefix is what the shared classifier reads as
        # a refused command (SERVICE_CALL_FAILED) when nothing more specific
        # matches.
        raise HomeAssistantCommandError(
            "Command failed: reading the exposed entities failed: "
            + core_error_message(_extract_ws_error(reply)),
            code=reply.get("error_code"),
        )
    exposed_entities = (reply.get("result") or {}).get("exposed_entities") or {}
    known: dict[str, tuple[str, dict[str, Any]]] = {}
    for eid, state_id in zip(entity_ids, state_ids, strict=True):
        key = state_id or (eid if eid in exposed_entities else None)
        if key is not None:
            known[eid] = (key, exposed_entities.get(key) or {})
    return known


async def get_entries_or_unregistered(
    client: Any, entity_ids: list[str]
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, tuple[str, dict[str, Any]]],
    dict[str, str],
]:
    """Registry entries, the ids without one that Home Assistant knows (as
    ``unregistered_lookup`` returns them), and the get_entries error of every
    id without a registry entry.

    When the lookup fails, each id without a registry entry gets that failure
    as its error, since whether Home Assistant knows it is still open.
    """
    found, errors = await get_entries_raw(client, entity_ids)
    missing = [eid for eid, err in errors.items() if err == _NOT_IN_REGISTRY]
    try:
        known = await unregistered_lookup(client, missing)
    except HomeAssistantError as exc:
        known = {}
        unchecked = (
            "no registry entry, and checking whether Home Assistant knows it "
            f"failed: {exc}"
        )
        errors.update(dict.fromkeys(missing, unchecked))
    return found, known, errors


def unregistered_row(entity_id: str, exposed_to: dict[str, Any]) -> dict[str, Any]:
    """ha_get_entity's answer for an entity without a registry entry."""
    return {
        "entity_id": entity_id,
        "entity_entry": None,
        "exposed_to": exposed_to,
        "is_exposed_anywhere": bool(exposed_to),
    }


async def _state_entity_id(client: Any, entity_id: str) -> str | None:
    """The id the entity's state carries, or None when it has no state (a 404)."""
    state = await get_entity_state_or_none(client, entity_id)
    if state is None:
        return None
    state_id: str = state["entity_id"]
    return state_id
