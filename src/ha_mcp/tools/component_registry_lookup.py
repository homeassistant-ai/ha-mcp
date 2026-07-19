"""Shared entity-registry reads over the ``ha_mcp_tools`` component gate.

Two helper-management paths resolve entities the same wasteful (or race-prone)
way today:

- The flow-helper delete path and the post-create wait poll both need EVERY
  entity bound to one ``config_entry_id`` (a ``utility_meter`` and its tariff
  sub-entities), and get them by dumping the WHOLE ``config/entity_registry/list``
  and filtering client-side.
- The simple-helper delete path resolves ONE entity's ``unique_id`` with a
  3-attempt exponential-backoff ``config/entity_registry/get`` loop, whose reason
  to exist is absorbing the registry-registration LAG between a helper's creation
  and its entity landing in the registry index.

When the component advertises the ``registry_lookup`` capability, both collapse
to a single in-process read: ``registry_lookup(config_entry_id=...)`` returns
every entity for one entry, ``registry_lookup(entity_ids=[...])`` returns the
rows for a set of ids (with a ``missing`` list for ids with no registry entry).
Each row is core's ``RegistryEntry.as_partial_dict`` VERBATIM — byte-identical to
a ``config/entity_registry/list`` element — so the consumers keep their existing
transforms over the raw shape. On the simple-delete path, ONE authoritative
in-process read plus the direct-id fallback replaces the 3-attempt retry loop —
NOT because the read is fresher (there was never a WS-timing race: the legacy
``config/entity_registry/get`` reads the same live registry over the same socket,
so an in-process read is equally subject to the registration LAG the loop
absorbed), but because a stale/missing resolve degrades to the direct-id delete
rather than a wrong one.

This module owns the caps-gated fetch so the routing discipline — probe caps,
send one frame, invalidate on ``unknown_command``, fall back to the legacy path
on any component error — lives in one place (the pattern ``component_devices``
established for the ``device_get`` / ``device_list`` capabilities). Both helpers
return ``None`` to mean "component unavailable — use the legacy path"; a component
that answers returns its payload. Per the uniform transport-fallback taxonomy, a
``HomeAssistantConnectionError`` — a pooled-WS drop, or a failed (re)connect —
is caught and mapped to ``None`` so the legacy path runs: the consumers' legacy reads (the whole-registry
``config/entity_registry/list`` dump and the per-id ``config/entity_registry/get``
retry loop) ride the ``send_websocket_message`` bridge, which returns
``{"success": False}`` rather than raising — so they do NOT die identically on a
pooled-WS drop, and letting a transport failure escape would skip the
``resolve_entities_via_component`` consumer's legacy retry loop entirely.
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

WS_REGISTRY_LOOKUP = "ha_mcp_tools/registry_lookup"


async def fetch_entities_for_config_entry_via_component(
    client: Any, config_entry_id: str
) -> list[dict[str, Any]] | None:
    """Every entity-registry row for one ``config_entry_id``; ``None`` ⇒ legacy.

    One ``registry_lookup(config_entry_id=...)`` read returning the component's
    ``entities`` list — each row the raw ``RegistryEntry.as_partial_dict`` shape,
    byte-identical to a ``config/entity_registry/list`` element and ALREADY
    filtered to the entry server-side (so the caller drops its client-side
    ``config_entry_id`` filter). Returns ``None`` — the caller falls back to the
    legacy whole-registry dump — when the component lacks the ``registry_lookup``
    capability, was downgraded (``unknown_command`` → invalidate the cached caps),
    errored (logged), or answered with a shape that is not an ``entities`` list.
    An AUTHORITATIVE empty result (the entry genuinely has no entities) comes back
    as a present empty list, kept distinct from that ``None`` miss.
    """
    caps = await get_component_caps(client)
    if not component_supports(caps, "registry_lookup"):
        return None
    try:
        ws = await get_websocket_client(url=client.base_url, token=client.token)
        raw = await ws.send_command(WS_REGISTRY_LOOKUP, config_entry_id=config_entry_id)
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning(
                "%s failed; fell back to legacy: %r", WS_REGISTRY_LOOKUP, exc
            )
        return None
    except Exception as exc:
        # HomeAssistantConnectionError / plain establish Exception → legacy (the
        # legacy entity_registry/list dump rides the bridge; see module
        # docstring). Never propagate a transport failure out of the read.
        logger.warning(
            "%s connection error; falling back to legacy: %r", WS_REGISTRY_LOOKUP, exc
        )
        return None
    result = raw.get("result")
    entities = result.get("entities") if isinstance(result, dict) else None
    if not isinstance(entities, list):
        logger.debug(
            "%s (config_entry_id) returned a malformed result (no 'entities' list); "
            "falling back to legacy",
            WS_REGISTRY_LOOKUP,
        )
        return None
    return entities


async def resolve_entities_via_component(
    client: Any, entity_ids: list[str]
) -> dict[str, Any] | None:
    """Resolve a set of entity_ids to registry rows; ``None`` ⇒ legacy.

    One ``registry_lookup(entity_ids=[...])`` read returning the component's
    ``{"entities": [...], "missing": [...]}`` payload — ``entities`` are the raw
    ``RegistryEntry.as_partial_dict`` rows for the ids that resolved,
    ``missing`` lists the ids with no registry entry (never silently dropped).
    Returns ``None`` — the caller falls back to its legacy per-id read — on
    capability miss, downgrade (``unknown_command`` → invalidate caps), error
    (logged), or a reply whose ``entities`` is not a list. Same error taxonomy and
    silent fallback as :func:`fetch_entities_for_config_entry_via_component`.
    """
    caps = await get_component_caps(client)
    if not component_supports(caps, "registry_lookup"):
        return None
    try:
        ws = await get_websocket_client(url=client.base_url, token=client.token)
        raw = await ws.send_command(WS_REGISTRY_LOOKUP, entity_ids=entity_ids)
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning(
                "%s failed; fell back to legacy: %r", WS_REGISTRY_LOOKUP, exc
            )
        return None
    except Exception as exc:
        # HomeAssistantConnectionError / plain establish Exception → legacy. The
        # simple-delete consumer's legacy per-id retry loop only runs when this
        # returns None, so an escaping transport failure would skip it entirely.
        logger.warning(
            "%s connection error; falling back to legacy: %r", WS_REGISTRY_LOOKUP, exc
        )
        return None
    result = raw.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("entities"), list):
        logger.debug(
            "%s (entity_ids) returned a malformed result (no 'entities' list); "
            "falling back to legacy",
            WS_REGISTRY_LOOKUP,
        )
        return None
    return result
