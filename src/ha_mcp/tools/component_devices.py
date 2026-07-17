"""Shared device-registry reads over the ``ha_mcp_tools`` component gate.

Several tools resolve a device the same wasteful way today — pull the ENTIRE
device registry and filter for one entry (``ha_get_device`` single lookup,
``ha_remove_device``'s body, the ``@with_auto_backup`` capture read, and
``ha_manage_radio``'s ``_resolve_ieee``). When the component advertises the
``device_get`` / ``device_list`` capabilities, that whole-registry dump becomes a
single in-process read: ``device_get`` returns one ``DeviceEntry.dict_repr`` by
id, ``device_list`` returns them all — each byte-identical to a
``config/device_registry/list`` element by construction (see
``custom_components/ha_mcp_tools/websocket_api.py``). Consumers keep their own
transforms over that raw shape.

This module owns the caps-gated fetch so the routing discipline — probe caps,
send one frame, invalidate on ``unknown_command``, fall back to the legacy path
on any component error — lives in one place instead of being duplicated per
consumer (the pattern ``tools_search._fetch_states_via_component`` established
for the ``states`` capability). Both helpers return ``None`` to mean "component
unavailable — use the legacy path"; a component that answers authoritatively
returns its payload (with ``device`` possibly ``None`` for "no such device").

``device_get`` also carries an opt-in per-device entity join
(:func:`fetch_device_entities_via_component`): with ``include_entities`` the
component returns the device's ``config/entity_registry/list``-shaped rows as a
SIBLING ``entities`` key, so a single-device lookup no longer dumps the whole
entity registry to list one device's entities. The join is additive within
schema_version 1, so the server tolerates its absence rather than depending on a
version bump. An older ``device_get`` that predates the param never round-trips
the entities half — the extra field is rejected by the command's
``PREVENT_EXTRA`` base schema, surfacing as an error that maps to the ``None``
miss — and, belt-and-suspenders, a response that carries the device but no
``entities`` key is treated the same: fall back to the legacy
``config/entity_registry/list`` for the entity half. Neither breaks the call.
"""

from __future__ import annotations

import logging
from typing import Any

from ..client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ..client.websocket_client import get_websocket_client
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)

logger = logging.getLogger(__name__)

WS_DEVICE_GET = "ha_mcp_tools/device_get"
WS_DEVICE_LIST = "ha_mcp_tools/device_list"


async def fetch_device_via_component(
    client: Any, device_id: str, *, include_entities: bool = False
) -> dict[str, Any] | None:
    """One ``ha_mcp_tools/device_get`` read; ``None`` ⇒ use the legacy path.

    Returns the component's ``{"device": <raw dict> | None}`` payload (the raw
    ``DeviceEntry.dict_repr`` for the id, byte-identical to a
    ``config/device_registry/list`` element) or ``None`` when the component lacks
    the ``device_get`` capability, was downgraded (``unknown_command`` →
    invalidate the cached caps), or errored (logged). A component that answers
    with ``{"device": None}`` is authoritative — the device does not exist — so
    the caller must distinguish that from a ``None`` return (which means "component
    unavailable, fall back"). Falls back **silently**, mirroring
    ``ha_get_state``: the legacy path returns the byte-identical device either way
    and the ``log.warning`` preserves operator visibility. A
    ``HomeAssistantConnectionError`` (pooled-WS drop) or the plain ``Exception``
    ``get_websocket_client()`` raises on a failed (re)connect is caught here and
    mapped to ``None``: the consumers' legacy paths ride the swallowing
    ``send_websocket_message`` bridge (registry tools) or a dedicated one-shot WS
    client (the auto-backup capture) — NOT this pooled socket — so a transport
    failure must fall back rather than block a tool / a wrapped write.

    With ``include_entities`` the payload also carries a sibling ``entities`` list
    (the device's ``config/entity_registry/list``-shaped rows), so a single-device
    lookup that needs the device's entities skips the whole-entity-registry dump.
    ``include_entities`` is only sent when true, so the frames of callers that need
    only the device (``_resolve_ieee`` / capture / remove) are unchanged.
    """
    caps = await get_component_caps(client)
    if not component_supports(caps, "device_get"):
        return None
    kwargs: dict[str, Any] = {"device_id": device_id}
    if include_entities:
        kwargs["include_entities"] = True
    try:
        ws = await get_websocket_client(url=client.base_url, token=client.token)
        raw = await ws.send_command(WS_DEVICE_GET, **kwargs)
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning("%s failed; fell back to legacy: %r", WS_DEVICE_GET, exc)
        return None
    except Exception as exc:
        # HomeAssistantConnectionError (pooled-WS drop) OR the plain Exception
        # get_websocket_client() raises when WebSocketManager can't (re)connect.
        # The legacy paths ride the swallowing send_websocket_message bridge / a
        # dedicated capture socket, not this pooled one, so fall back to legacy.
        logger.warning(
            "%s connection error; falling back to legacy: %r", WS_DEVICE_GET, exc
        )
        return None
    result = raw.get("result")
    if not isinstance(result, dict) or "device" not in result:
        logger.debug(
            "%s returned a malformed result (missing 'device' key); falling back to legacy",
            WS_DEVICE_GET,
        )
        return None
    return result


async def fetch_device_entities_via_component(
    client: Any, device_id: str
) -> list[dict[str, Any]] | None:
    """The device's entity rows via ``device_get(include_entities=True)``; ``None`` ⇒ legacy.

    Returns the ``config/entity_registry/list``-shaped rows bound to ``device_id``
    (disabled included) so a per-device entity lookup avoids the whole-registry
    dump. Returns ``None`` — the caller falls back to
    ``config/entity_registry/list`` — when the component can't serve ``device_get``
    at all, OR when it served the device but the response carries no ``entities``
    key (an older ``device_get`` predating ``include_entities``, or any component
    that does not round-trip the entities half): the entity join is additive, so
    its absence degrades to legacy rather than silently reporting zero entities.
    An AUTHORITATIVE empty result — the component honored ``include_entities`` and
    the device has no entities, or no such device — comes back as a present empty
    list, kept distinct from that ``None`` miss.
    """
    result = await fetch_device_via_component(client, device_id, include_entities=True)
    if result is None:
        return None
    entities = result.get("entities")
    if not isinstance(entities, list):
        logger.debug(
            "%s served the device but no 'entities' list (additive join absent); "
            "falling back to legacy entity_registry/list",
            WS_DEVICE_GET,
        )
        return None
    return entities


async def fetch_device_list_via_component(client: Any) -> dict[str, Any] | None:
    """One ``ha_mcp_tools/device_list`` read; ``None`` ⇒ use the legacy path.

    Returns the component's ``{"devices": [<raw dict>, ...]}`` payload (each a raw
    ``DeviceEntry.dict_repr``, the in-process equivalent of
    ``config/device_registry/list``) or ``None`` on capability miss, downgrade
    (``unknown_command`` → invalidate caps), command error/timeout, or a
    connection-establishment failure (all logged) — same error-taxonomy and silent
    fallback as :func:`fetch_device_via_component`.
    """
    caps = await get_component_caps(client)
    if not component_supports(caps, "device_list"):
        return None
    try:
        ws = await get_websocket_client(url=client.base_url, token=client.token)
        raw = await ws.send_command(WS_DEVICE_LIST)
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning("%s failed; fell back to legacy: %r", WS_DEVICE_LIST, exc)
        return None
    except Exception as exc:
        # HomeAssistantConnectionError / plain establish Exception → legacy (the
        # legacy device list rides the swallowing send_websocket_message bridge).
        logger.warning(
            "%s connection error; falling back to legacy: %r", WS_DEVICE_LIST, exc
        )
        return None
    result = raw.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("devices"), list):
        logger.debug(
            "%s returned a malformed result (no 'devices' list); falling back to legacy",
            WS_DEVICE_LIST,
        )
        return None
    return result
