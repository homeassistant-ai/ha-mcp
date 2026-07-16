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
    client: Any, device_id: str
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
    ``HomeAssistantConnectionError`` (WS down) is not caught here, so it
    propagates to the caller's own error handling — the legacy path shares the
    same socket and would fail identically.
    """
    caps = await get_component_caps(client)
    if not component_supports(caps, "device_get"):
        return None
    try:
        ws = await get_websocket_client(url=client.base_url, token=client.token)
        raw = await ws.send_command(WS_DEVICE_GET, device_id=device_id)
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning("%s failed; fell back to legacy: %r", WS_DEVICE_GET, exc)
        return None
    result = raw.get("result")
    if not isinstance(result, dict) or "device" not in result:
        return None
    return result


async def fetch_device_list_via_component(client: Any) -> dict[str, Any] | None:
    """One ``ha_mcp_tools/device_list`` read; ``None`` ⇒ use the legacy path.

    Returns the component's ``{"devices": [<raw dict>, ...]}`` payload (each a raw
    ``DeviceEntry.dict_repr``, the in-process equivalent of
    ``config/device_registry/list``) or ``None`` on capability miss, downgrade
    (``unknown_command`` → invalidate caps), or error (logged) — same
    error-taxonomy and silent fallback as :func:`fetch_device_via_component`.
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
    result = raw.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("devices"), list):
        return None
    return result
