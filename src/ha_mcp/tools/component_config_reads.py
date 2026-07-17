"""Config-resolution + reference-data reads over the ``ha_mcp_tools`` component gate.

Two consumers in the automation/script/scene family pay for a whole-collection
fetch to answer a single question:

- The set/remove/post-write resolvers (``_resolve_scene_entity_id`` /
  ``_resolve_automation_entity_id``) map a storage id (a scene's ``unique_id``,
  an automation's config id) to its live ``entity_id`` — the scene resolver by
  dumping the ENTIRE ``config/entity_registry/list`` (with a 0.2 s sleep + retry
  to absorb post-upsert registration lag), the automation resolver by scanning
  the WHOLE ``get_states()`` state machine. When the component advertises
  ``entity_lookup``, a single ``ha_mcp_tools/entity_lookup(unique_id=, domain=)``
  frame returns just the matching registry entries — a hit is authoritative the
  instant it returns (no settle). An in-process read removes the network latency
  but NOT HA's async entity-registration lag, so the scene resolver still
  rechecks an EMPTY result ONCE after the same short delay before its naive
  fallback (the automation resolver has no post-upsert lag exposure — its only
  routed call site resolves an already-registered entity before a delete).
- The reference validator (``validate_config_references``) fetches BOTH
  ``client.get_services()`` and ``client.get_states()`` on every automation/script
  write purely to build a name index. When the component advertises
  ``reference_data``, one ``ha_mcp_tools/reference_data`` frame returns the
  REST-shaped service catalog + the entity-id universe together.

This module owns the caps-gated fetch so the routing discipline — probe caps,
send one frame, invalidate on ``unknown_command``, fall back to the legacy path
on any component error — lives in one place instead of being duplicated per
consumer (the pattern ``component_devices`` established for the ``device_get`` /
``device_list`` capabilities). Both helpers return ``None`` to mean "component
unavailable — use the legacy path"; a component that answers authoritatively
returns its payload (an entity_lookup with an EMPTY ``matches`` list is
authoritative — "no registry entry with that unique_id" — kept distinct from the
``None`` miss).

Both helpers apply the uniform transport-fallback taxonomy: a
``HomeAssistantConnectionError`` off ``send_command`` — and the plain
``Exception`` ``get_websocket_client()`` raises when ``WebSocketManager`` cannot
(re)build the pooled socket — are caught and mapped to ``None`` so the legacy
path runs, NEVER propagated out of the read. Neither consumer's legacy path dies
identically on a pooled-WS drop, so propagating would abort work the legacy path
still completes:

- ``fetch_entity_lookup_via_component``'s consumers degrade gracefully. The scene
  resolver's ``config/entity_registry/list`` dump rides
  ``client.send_websocket_message`` — the swallowing bridge that returns
  ``{"success": False}`` instead of raising, so the resolver walks its retry loop
  to the naive ``scene.{id}`` fallback — and the automation resolver scans REST
  ``get_states()`` and additionally catches broadly → ``None``. Its only routed
  call sites run AFTER a scene/automation upsert commits or BEFORE a REST delete,
  so an escaping transport error would report a landed write as failed / abort a
  delete the REST path would have finished.
- ``fetch_reference_data_via_component``'s legacy path is the REST
  ``get_services()`` / ``get_states()`` pair; an escaping transport error would
  make ``validate_config_references`` hit its swallow-all fetch guard and skip
  EVERY reference warning even when REST is up.

**GET-path invariant:** the automation/script/scene *config-get* tools must never
route through the component — their in-process ``raw_config`` freshness lags the
config file between a write and the next completed reload. The resolvers gate the
whole component branch (caps probe included) behind an explicit ``allow_component``
flag that only the set/remove/post-write call sites pass, so a get never even
probes caps. ``TestConfigGetSeam`` pins this.
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

WS_ENTITY_LOOKUP = "ha_mcp_tools/entity_lookup"
WS_REFERENCE_DATA = "ha_mcp_tools/reference_data"


async def fetch_entity_lookup_via_component(
    client: Any,
    unique_id: str,
    *,
    domain: str | None = None,
) -> list[dict[str, Any]] | None:
    """One ``ha_mcp_tools/entity_lookup`` read; ``None`` ⇒ use the legacy path.

    Returns the component's ``matches`` list — every entity-registry entry whose
    ``unique_id`` equals ``unique_id`` (each ``{entity_id, unique_id, platform,
    domain, config_entry_id, categories, disabled_by, hidden_by}``), optionally
    narrowed by the entity's own ``domain``. Multiple matches (one ``unique_id``
    across platforms) are all returned — the caller picks. An AUTHORITATIVE empty
    list (no registry entry with that unique_id) is kept distinct from the
    ``None`` miss (component unavailable → legacy).

    ``None`` on capability miss, downgrade (``unknown_command`` → invalidate the
    cached caps), command error/timeout (logged), a connection-establishment
    failure (logged — see the module docstring: the resolvers' legacy paths ride
    the swallowing WS bridge / REST, NOT this pooled socket, so a transport
    failure must fall back rather than abort a landed write), or a shape-drift
    payload (no ``matches`` list). Same error-taxonomy and silent fallback as
    ``component_devices.fetch_device_via_component``.
    """
    caps = await get_component_caps(client)
    if not component_supports(caps, "entity_lookup"):
        return None
    kwargs: dict[str, Any] = {"unique_id": unique_id}
    if domain is not None:
        kwargs["domain"] = domain
    try:
        ws = await get_websocket_client(url=client.base_url, token=client.token)
        raw = await ws.send_command(WS_ENTITY_LOOKUP, **kwargs)
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning("%s failed; fell back to legacy: %r", WS_ENTITY_LOOKUP, exc)
        return None
    except Exception as exc:
        # HomeAssistantConnectionError (pooled-WS drop) OR the plain Exception
        # get_websocket_client() raises when WebSocketManager can't (re)connect.
        # The resolvers' legacy paths do NOT die identically (see module docstring),
        # so route to legacy rather than escape.
        logger.warning(
            "%s connection error; falling back to legacy: %r", WS_ENTITY_LOOKUP, exc
        )
        return None
    result = raw.get("result")
    matches = result.get("matches") if isinstance(result, dict) else None
    if not isinstance(matches, list):
        logger.debug(
            "%s returned a malformed result (no 'matches' list); falling back to legacy",
            WS_ENTITY_LOOKUP,
        )
        return None
    return matches


async def fetch_reference_data_via_component(
    client: Any, *, include_states: bool = True
) -> dict[str, Any] | None:
    """One ``ha_mcp_tools/reference_data`` read; ``None`` ⇒ use the legacy path.

    Returns ``{"services": <REST /api/services list shape>, "entity_ids":
    [...]}`` — the service catalog ``build_service_index`` consumes verbatim and
    the entity-id universe ``build_entity_set`` reduces to a set — replacing the
    reference validator's ``asyncio.gather(get_services(), get_states())``.
    ``include_states=False`` suppresses the entity-id half (services only).

    ``None`` on capability miss, downgrade (``unknown_command`` → invalidate the
    cached caps), command error/timeout (logged), a connection-establishment
    failure (logged — see below), or a shape-drift payload (``services`` /
    ``entity_ids`` not both lists) — the caller falls back to the legacy REST
    fetches.

    Like every component fetch helper, a transport failure routes to ``None``
    (→ legacy REST) rather than escaping; the reference validator's legacy path is
    the REST ``get_services()`` / ``get_states()`` pair. The catch is
    broad because ``get_websocket_client()`` raises a plain ``Exception`` (not
    ``HomeAssistantConnectionError``) when ``WebSocketManager`` cannot build the
    socket; letting it escape would make ``validate_config_references`` hit its
    swallow-all fetch guard and skip ALL reference warnings even when REST is up.
    Mirrors ``get_component_caps``' own broad-catch precedent.
    """
    caps = await get_component_caps(client)
    if not component_supports(caps, "reference_data"):
        return None
    kwargs: dict[str, Any] = {}
    if not include_states:
        kwargs["include_states"] = False
    try:
        ws = await get_websocket_client(url=client.base_url, token=client.token)
        raw = await ws.send_command(WS_REFERENCE_DATA, **kwargs)
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning("%s failed; fell back to legacy: %r", WS_REFERENCE_DATA, exc)
        return None
    except Exception as exc:
        # Legacy is REST (get_services/get_states), NOT the pooled WS, so a
        # pooled-WS drop (HomeAssistantConnectionError) OR get_websocket_client()
        # raising a plain Exception when WebSocketManager can't (re)connect must
        # route to the REST fetch rather than escape into the validator's
        # swallow-all guard (which would skip every reference warning).
        logger.warning(
            "%s connection error; falling back to REST legacy: %r",
            WS_REFERENCE_DATA,
            exc,
        )
        return None
    result = raw.get("result")
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("services"), list)
        or not isinstance(result.get("entity_ids"), list)
    ):
        logger.debug(
            "%s returned a malformed result (services/entity_ids not both lists); "
            "falling back to legacy",
            WS_REFERENCE_DATA,
        )
        return None
    return result
