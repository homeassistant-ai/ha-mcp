"""Voice-assistant exposure reads for ``ha_get_entity``.

Core keeps exposure in two places: an entity with a registry entry stores it in
the entry's ``options`` (which ``ha_get_entity`` already returns), and an entity
without one in Core's separate exposed-entities store. Both are served here:
the listing of exposed entities and the rows for entities without a registry
entry. Writes go through ``ha_set_entity`` (``expose_to``).
"""

import logging
from collections import Counter
from typing import Any

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ..client.websocket_client import get_websocket_client
from ..errors import create_error_response
from .component_api import (
    DEVICE_REGISTRY_CHILD_SEMANTICS,
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .helpers import raise_tool_error, ws_failure_code

logger = logging.getLogger(__name__)

# The ha_mcp_tools/exposure WS command: the legacy expose_entity/list map PLUS a
# sibling entity_info enrichment (names/areas). Named once so the routing helper
# and its tests agree on the wire string.
WS_EXPOSURE = "ha_mcp_tools/exposure"


async def _fetch_exposure_via_component(client: Any) -> dict[str, Any] | None:
    """One ``ha_mcp_tools/exposure`` list read; ``None`` ⇒ read Core's list.

    Returns the component payload — ``{exposed_entities: {id: {assistant:
    True}}, entity_info: {id: {...}}}`` — where ``exposed_entities`` holds the
    same entries as Core's ``homeassistant/expose_entity/list`` map. ``None`` on
    capability miss, downgrade (``unknown_command`` → invalidate the cached
    caps), command error/timeout (logged), or a connection-establishment failure
    (logged) — the caller falls back to Core's list. A
    ``HomeAssistantConnectionError`` — a pooled-WS drop, or a failed (re)connect
    — is caught here and mapped to ``None``: Core's list read rides the
    ``send_websocket_message`` bridge, which answers a component-side fault with
    ``{"success": False}`` - so a fault here falls back rather than escapes. It
    is the SAME pooled connection, so a genuinely dead transport raises there
    too (#1947) instead of degrading. Same caps-gate discipline as
    ``component_devices.fetch_device_via_component``.
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
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning("%s failed; fell back to legacy: %r", WS_EXPOSURE, exc)
        return None
    except Exception as exc:  # noqa: BLE001
        # HomeAssistantConnectionError or any other error → Core's list (it
        # rides the bridge).
        logger.warning(
            "%s connection error; falling back to legacy: %r", WS_EXPOSURE, exc
        )
        return None
    result = raw.get("result")
    if not isinstance(result, dict) or "exposed_entities" not in result:
        return None
    return result


async def read_exposure(client: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Core's exposed-entity map, plus each entity's names when the component serves them.

    Prefers the component's in-process read when advertised: the same map as
    ``expose_entity/list`` plus an ``entity_info`` enrichment (names/areas), so
    a caller needs no second ha_search to name an exposed entity. Falls back to
    Core's list on any miss (taxonomy in ``_fetch_exposure_via_component``),
    with no ``entity_info``.
    """
    component = await _fetch_exposure_via_component(client)
    if component is not None:
        return (
            component.get("exposed_entities") or {},
            component.get("entity_info") or {},
        )
    result = await client.send_websocket_message(
        {"type": "homeassistant/expose_entity/list"}
    )
    if not result.get("success"):
        raise_tool_error(
            create_error_response(
                ws_failure_code(result),
                f"Failed to get exposure settings: {result.get('error')}",
            )
        )
    return (result.get("result") or {}).get("exposed_entities") or {}, {}


def list_exposures(
    exposed_entities: dict[str, Any],
    entity_info: dict[str, Any],
    assistant: str | None,
) -> dict[str, Any]:
    """The exposed entities, all or those exposed to one assistant."""
    filtered = exposed_entities
    if assistant:
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
    if not assistant:
        response["summary"] = dict(
            Counter(asst for settings in filtered.values() for asst in settings)
        )
    if entity_info:
        response["entity_info"] = {
            eid: entity_info[eid] for eid in filtered if eid in entity_info
        }
    return response


async def unregistered_exposure(
    client: Any, entity_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """``exposed_to`` for each id without a registry entry that Home Assistant knows.

    Such an entity's exposure lives in Core's exposed-entities store. An id
    counts as known when that store lists it as exposed or it has a state; any
    other id is left out, so the caller reports it as not found.
    """
    if not entity_ids:
        return {}
    exposed_entities, _ = await read_exposure(client)
    rows: dict[str, dict[str, Any]] = {}
    for eid in entity_ids:
        if eid not in exposed_entities and not await _has_state(client, eid):
            continue
        exposed_to = exposed_entities.get(eid) or {}
        rows[eid] = {
            "entity_id": eid,
            "entity_entry": None,
            "exposed_to": exposed_to,
            "is_exposed_anywhere": bool(exposed_to),
        }
    return rows


async def _has_state(client: Any, entity_id: str) -> bool:
    """Whether the entity has a state; a 404 is the only "no"."""
    try:
        await client.get_entity_state(entity_id)
    except HomeAssistantAPIError as exc:
        if exc.status_code == 404:
            return False
        raise
    return True
