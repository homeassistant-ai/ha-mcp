"""Resolve storage keys after writes while Home Assistant registers entities."""

import asyncio
import logging
import time
from typing import Any, Literal

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from .component_config_reads import fetch_entity_lookup_via_component

logger = logging.getLogger(__name__)

# A bounded registration budget, not a fixed delay: existing entries return on
# the first lookup. Keep this separate from the subsequent state-availability
# wait, which cannot discover a name-derived or registry-renamed entity ID.
RESOLVE_TIMEOUT = 5.0


def _matching_entity_id(
    entries: list[dict[str, Any]], storage_key: str, domain: Literal["scene", "script"]
) -> str | None:
    """UI scenes belong to platform homeassistant; scripts belong to script."""
    platform = "homeassistant" if domain == "scene" else "script"
    for entry in entries:
        entity_id = entry.get("entity_id") or ""
        if (
            entry.get("unique_id") == storage_key
            and entry.get("platform") == platform
            and entity_id.startswith(f"{domain}.")
        ):
            return str(entity_id)
    return None


async def _lookup_entity_id(
    client: Any, storage_key: str, domain: Literal["scene", "script"]
) -> str | None:
    """Read once; an unavailable component falls back to the legacy registry."""
    matches = await fetch_entity_lookup_via_component(
        client, storage_key, domain=domain
    )
    if matches is None:
        if domain == "script":
            direct = await client.send_websocket_message(
                {
                    "type": "config/entity_registry/get",
                    "entity_id": f"script.{storage_key}",
                }
            )
            row = direct.get("result")
            if direct.get("success") and isinstance(row, dict):
                entity_id = _matching_entity_id([row], storage_key, domain)
                if entity_id is not None:
                    return entity_id
        listing = await client.send_websocket_message(
            {"type": "config/entity_registry/list"}
        )
        if listing.get("success") is False:
            raise HomeAssistantAPIError(
                f"Entity registry lookup failed: {listing.get('error')} "
                f"(code={listing.get('error_code')})"
            )
        matches = listing.get("result") or []
    return _matching_entity_id(matches, storage_key, domain)


async def resolve_entity_id_after_write(
    client: Any,
    storage_key: str,
    domain: Literal["scene", "script"],
    *,
    timeout: float | None = None,
    poll_interval: float = 0.2,
    fallback_entity_id: str | None = None,
) -> str:
    """Poll for the actual entity ID before a post-write wait/category update.

    An empty registry read can precede asynchronous registration even inside
    the component. Retry against one deadline, checking component availability
    on every attempt. A positive budget includes in-flight lookups and sleeps;
    zero requests one lookup without a time limit, for test fixtures. Known
    API failures retain the best-effort fallback. Errors escaping the component
    adapter propagate unless listed below; cancellation always propagates.
    ``fallback_entity_id`` overrides the constructed ID on failure; registry
    matching always uses the storage key.

    Config reads and removals must use their existing resolution paths. Bulk
    writes with neither a state wait nor a category update should skip this
    helper entirely. The optional budget and interval support deterministic
    tests without extending production waits.
    """
    storage_key = storage_key.removeprefix(f"{domain}.")
    fallback = fallback_entity_id or f"{domain}.{storage_key}"
    budget = RESOLVE_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + budget
    try:
        async with asyncio.timeout(budget if budget > 0 else None):
            while True:
                entity_id = await _lookup_entity_id(client, storage_key, domain)
                if entity_id is not None:
                    return entity_id
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(poll_interval, remaining))
                if time.monotonic() >= deadline:
                    break
    except (
        TimeoutError,
        HomeAssistantAPIError,
        HomeAssistantAuthError,
        HomeAssistantConnectionError,
    ) as exc:
        logger.warning(
            "Post-write registry resolve failed for %s.%s (%s: %s); using %s",
            domain,
            storage_key,
            type(exc).__name__,
            exc,
            fallback,
        )
    else:
        logger.debug(
            "Post-write registry budget exhausted for %s.%s; using %s",
            domain,
            storage_key,
            fallback,
        )
    return fallback
