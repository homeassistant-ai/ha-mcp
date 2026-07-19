"""Shared registry reads over the ``ha_mcp_tools`` component gate.

Several read paths pull an entire HA registry and filter client-side for a
handful of entries: ``ha_list_floors_areas`` (two CONCURRENT but independent
``config/area_registry/list`` / ``config/floor_registry/list`` calls — a
TOCTOU window where a registry change between the two reads can misclassify
an area as orphaned/unassigned) and the auto-backup capture reads for label /
category / area / floor (``backup_manager.py``, one whole-registry dump per
captured entity). When the component advertises the ``registries``
capability, all of that becomes ONE in-process read: ``ha_mcp_tools/registries``
returns exactly the requested registry kinds (``area`` / ``floor`` / ``label``
/ ``category``) as their FULL-FIELD ``config/<x>_registry/list``-shaped rows —
byte-compatible with the legacy WS list responses the consumers already parse
(see ``custom_components/ha_mcp_tools/websocket_api.py::_do_registries``).

This module owns the caps-gated fetch so the routing discipline — probe caps,
send one frame, invalidate on ``unknown_command``, fall back to the legacy
path on any component error — lives in one place instead of being duplicated
per consumer, mirroring ``component_devices.fetch_device_via_component``.
Returns ``None`` to mean "component unavailable — use the legacy path"; a
component that answers authoritatively returns its payload (only the keys for
the requested registry kinds present).
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

WS_REGISTRIES = "ha_mcp_tools/registries"

_LIST_SLICES = {"area": "areas", "floor": "floors", "label": "labels"}


def _has_valid_shape(
    result: dict[str, Any],
    registries: list[str],
    category_scopes: list[str] | None,
) -> bool:
    """True if every requested slice in ``result`` is its expected list/dict shape.

    Mirrors the legacy path's ``_require_list``-style guard
    (``backup_manager.py``) but never raises — an unexpected shape here just
    means "don't trust this component response", so the caller falls back to
    the legacy WS list(s) instead of surfacing a malformed dump as a
    confident tool result. A requested slice missing from ``result``
    entirely counts as a mismatch too: the contract is that a requested kind
    is always present (empty list, never absent) when the component answers.
    """
    for kind in registries:
        if kind == "category":
            categories = result.get("categories")
            if not isinstance(categories, dict):
                return False
            for scope in category_scopes or ():
                if not isinstance(categories.get(scope), list):
                    return False
            continue
        key = _LIST_SLICES.get(kind)
        if key is not None and not isinstance(result.get(key), list):
            return False
    return True


async def fetch_registries_via_component(
    client: Any,
    registries: list[str],
    *,
    category_scopes: list[str] | None = None,
) -> dict[str, Any] | None:
    """One ``ha_mcp_tools/registries`` read; ``None`` ⇒ use the legacy path.

    Returns the component's ``{areas: [...], floors: [...], labels: [...],
    categories: {scope: [...]}}`` payload — only the keys for the requested
    ``registries`` kinds are present (a kind not requested is absent, never an
    empty list, so a caller can't confuse "not asked for" with "asked for,
    empty"). ``category`` additionally consults ``category_scopes``: categories
    are scoped and the component REQUIRES a non-empty scope list for a category
    request, so an omitted/empty list makes the component raise
    ``HomeAssistantError`` — which this helper logs and maps to ``None`` (legacy
    fallback), NOT ``{"categories": {}}``. Callers requesting ``category`` must
    pass scopes.

    ``None`` on capability miss, downgrade (``unknown_command`` → invalidate
    the cached caps), command error/timeout (logged), a connection-establishment
    failure (logged), or a malformed response — the outer ``result`` isn't a
    dict, or a requested slice isn't its expected list/dict shape (logged; see
    ``_has_valid_shape``) — the caller falls back to its legacy WS list call(s)
    in every case. A ``HomeAssistantConnectionError`` — a pooled-WS drop, or a
    failed (re)connect — is caught here and mapped to ``None``: the auto-backup capture fetchers'
    legacy ``_ws_send`` builds a DEDICATED one-shot WS client (which can succeed
    while the pooled socket is wedged) under a best-effort contract that requires
    warn-and-skip, never a blocked write; ``ha_list_floors_areas``' legacy rides
    the ``send_websocket_message`` bridge. Neither dies identically on
    a pooled-WS drop, so a transport failure must fall back rather than escape.
    """
    caps = await get_component_caps(client)
    if not component_supports(caps, "registries"):
        return None
    kwargs: dict[str, Any] = {"registries": list(registries)}
    if category_scopes:
        kwargs["category_scopes"] = list(category_scopes)
    try:
        ws = await get_websocket_client(url=client.base_url, token=client.token)
        raw = await ws.send_command(WS_REGISTRIES, **kwargs)
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning("%s failed; fell back to legacy: %r", WS_REGISTRIES, exc)
        return None
    except Exception as exc:
        # HomeAssistantConnectionError: a pooled-WS drop or a failed
        # (re)connect. The capture
        # fetchers use a dedicated one-shot socket and forbid a blocked write, so
        # this must fall back to legacy rather than escape into the write path.
        logger.warning(
            "%s connection error; falling back to legacy: %r", WS_REGISTRIES, exc
        )
        return None
    result = raw.get("result")
    if not isinstance(result, dict):
        return None
    if not _has_valid_shape(result, registries, category_scopes):
        logger.warning(
            "%s returned an unexpected shape; fell back to legacy", WS_REGISTRIES
        )
        return None
    return result
