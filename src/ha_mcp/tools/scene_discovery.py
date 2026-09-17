"""Bounded scene discovery with verified storage keys and optional body search."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantConnectionError,
    SceneResolution,
)
from ..client.websocket_client import get_websocket_client
from .component_api import (
    DEVICE_REGISTRY_CHILD_SEMANTICS,
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .component_registry_lookup import resolve_entities_via_component

logger = logging.getLogger(__name__)
CONFIG_SCAN_TIMEOUT = 10.0
CONFIG_SCAN_CONCURRENCY = 4
REGISTRY_CHUNK_SIZE = 500


async def _component_inventory(client: Any) -> list[dict[str, Any]] | None:
    """Enumerate scenes through the existing capability-gated search command."""
    try:
        caps = await get_component_caps(client)
        if not (
            component_supports(caps, "search")
            and component_supports(caps, DEVICE_REGISTRY_CHILD_SEMANTICS)
        ):
            return None
        assert caps is not None
        advertised = caps.limits.get("max_results", 500)
        limit = (
            min(advertised, 500)
            if isinstance(advertised, int)
            and not isinstance(advertised, bool)
            and advertised > 0
            else 500
        )
        async with asyncio.timeout(CONFIG_SCAN_TIMEOUT):
            ws = await get_websocket_client(
                url=client.base_url,
                token=client.token,
                verify_ssl=getattr(client, "verify_ssl", None),
            )
            return await _component_inventory_pages(ws, limit)
    except Exception as exc:
        # Optional component reads always retain the independent REST fallback.
        if is_unknown_command(exc):
            invalidate_caps(client)
        logger.debug("Scene inventory component read fell back to REST: %r", exc)
        return None


async def _component_inventory_pages(
    ws: Any, limit: int
) -> list[dict[str, Any]] | None:
    """Read every inventory page; discard incomplete or malformed snapshots."""
    rows: dict[str, dict[str, Any]] = {}
    offset = 0
    while True:
        raw = await ws.send_command(
            "ha_mcp_tools/search",
            search_types=["entity"],
            domain_filter="scene",
            include_hidden=True,
            limit=limit,
            offset=offset,
        )
        result = raw.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("entities"), list):
            return None
        if not isinstance(result.get("entity_has_more"), bool):
            return None
        if result.get("partial") or result.get("diagnostics"):
            return None
        page = result["entities"]
        if not all(
            isinstance(row, dict) and str(row.get("entity_id", "")).startswith("scene.")
            for row in page
        ):
            return None
        previous_count = len(rows)
        rows.update((row["entity_id"], row) for row in page)
        if not result.get("entity_has_more"):
            return list(rows.values())
        if len(rows) == previous_count:
            return None
        offset += len(page)


async def _registry_rows(
    client: Any, entity_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Read only scene registry entries when the component is available."""
    rows: list[dict[str, Any]] = []
    for start in range(0, len(entity_ids), REGISTRY_CHUNK_SIZE):
        result = await resolve_entities_via_component(
            client, entity_ids[start : start + REGISTRY_CHUNK_SIZE]
        )
        if result is None:
            raw = await client.send_websocket_message(
                {"type": "config/entity_registry/list"}
            )
            if raw.get("success") is False or not isinstance(raw.get("result"), list):
                raise HomeAssistantAPIError("Could not read the scene entity registry")
            rows = raw["result"]
            break
        rows.extend(result["entities"])
    requested = set(entity_ids)
    return {row["entity_id"]: row for row in rows if row.get("entity_id") in requested}


async def _inventory(client: Any) -> list[dict[str, Any]]:
    """Join names and owning platforms without treating vendor IDs as storage IDs."""
    states = await _component_inventory(client)
    if states is None:
        states = [
            row
            for row in await client.get_states()
            if row["entity_id"].startswith("scene.")
        ]
    registry = await _registry_rows(client, [row["entity_id"] for row in states])
    rows = []
    for state in states:
        entity_id = state["entity_id"]
        entry = registry.get(entity_id, {})
        platform = entry.get("platform")
        integration_managed = bool(platform and platform != "homeassistant")
        rows.append(
            {
                "entity_id": entity_id,
                "name": state.get("friendly_name")
                or state.get("attributes", {}).get("friendly_name")
                or entry.get("name")
                or entity_id,
                "platform": platform,
                "scene_id": None,
                "config_available": False if integration_managed else None,
                "config_status": "integration_managed"
                if integration_managed
                else "not_checked",
                "_storage_id": entry.get("unique_id")
                if platform == "homeassistant"
                else None,
            }
        )
    return sorted(rows, key=lambda row: row["entity_id"])


def _matches_identity(row: dict[str, Any], query: str) -> bool:
    """Match scene names, entity IDs, and homeassistant storage-key candidates."""
    return any(
        query in str(row.get(key) or "").casefold()
        for key in ("entity_id", "name", "_storage_id")
    )


async def _read_config(client: Any, row: dict[str, Any], query: str) -> None:
    """Verify editable storage and search the exact body without returning it."""
    if row["config_status"] == "integration_managed":
        return
    storage_id = row["_storage_id"]
    resolution = SceneResolution(
        storage_key=storage_id or row["entity_id"].removeprefix("scene."),
        registry_hit=bool(storage_id),
        platform=row["platform"],
    )
    try:
        envelope = await client.get_scene_config(
            row["entity_id"], resolution=resolution
        )
    except HomeAssistantAPIError as exc:
        if exc.status_code == 404:
            row.update(config_available=False, config_status="not_in_storage")
        else:
            row["config_status"] = "failed"
            logger.debug(
                "Scene config discovery failed for %s: %r", row["entity_id"], exc
            )
        return
    except (TimeoutError, OSError, HomeAssistantConnectionError) as exc:
        row["config_status"] = "failed"
        logger.debug("Scene config discovery failed for %s: %r", row["entity_id"], exc)
        return
    if not isinstance(envelope, dict):
        row["config_status"] = "failed"
        logger.debug(
            "Scene config discovery returned a malformed envelope for %s",
            row["entity_id"],
        )
        return
    config = envelope.get("config", envelope)
    if not isinstance(config, dict):
        row["config_status"] = "failed"
        logger.debug(
            "Scene config discovery returned a malformed config for %s",
            row["entity_id"],
        )
        return
    try:
        match_in_config = bool(
            query and query in json.dumps(config, ensure_ascii=False).casefold()
        )
    except (TypeError, ValueError) as exc:
        row["config_status"] = "failed"
        logger.debug(
            "Scene config discovery could not serialize %s: %r",
            row["entity_id"],
            exc,
        )
        return
    row.update(
        scene_id=envelope.get("scene_id") or config.get("id") or resolution.storage_key,
        config_available=True,
        config_status="available",
        match_in_config=match_in_config,
    )


async def _scan_configs(
    client: Any, rows: list[dict[str, Any]], query: str
) -> dict[str, int]:
    """Bound concurrent config reads and cancel outstanding work at the deadline."""
    candidates = iter(
        row for row in rows if row["config_status"] != "integration_managed"
    )

    async def worker() -> None:
        for row in candidates:
            await _read_config(client, row, query)

    tasks = [asyncio.create_task(worker()) for _ in range(CONFIG_SCAN_CONCURRENCY)]
    try:
        async with asyncio.timeout(CONFIG_SCAN_TIMEOUT):
            await asyncio.gather(*tasks)
    except TimeoutError:
        pass
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return {
        "checked": sum(row["config_status"] == "available" for row in rows),
        "failed": sum(row["config_status"] == "failed" for row in rows),
        "not_scanned": sum(row["config_status"] == "not_checked" for row in rows),
        "not_in_storage": sum(row["config_status"] == "not_in_storage" for row in rows),
        "integration_managed": sum(
            row["config_status"] == "integration_managed" for row in rows
        ),
    }


async def discover_scenes(
    client: Any, *, query: str | None, search_in_config: bool, limit: int, offset: int
) -> dict[str, Any]:
    """List compact scene metadata, optionally matching full storage attribute values."""
    rows = await _inventory(client)
    query_text = (query or "").strip().casefold()
    content_search = bool(query_text and search_in_config)
    for row in rows:
        row["match_in_name"] = _matches_identity(row, query_text)
        row["match_in_config"] = False
    if not content_search:
        rows = [row for row in rows if row["match_in_name"]]
    scan = await _scan_configs(
        client,
        rows if content_search else rows[offset : offset + limit],
        query_text if content_search else "",
    )
    if content_search:
        rows = [row for row in rows if row["match_in_name"] or row["match_in_config"]]
    page = rows[offset : offset + limit]
    has_more = offset + len(page) < len(rows)
    result: dict[str, Any] = {
        "success": True,
        "action": "list",
        "scenes": [
            {key: value for key, value in row.items() if not key.startswith("_")}
            for row in page
        ],
        "count": len(page),
        "total": len(rows),
        "total_is_exact": not (
            content_search
            and (scan["failed"] or scan["not_scanned"] or scan["not_in_storage"])
        ),
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
        "next_offset": offset + len(page) if has_more else None,
        "config_scan": scan,
    }
    if content_search:
        result["config_search_scope"] = (
            "Home Assistant editable storage scenes; integration-managed and raw YAML bodies are unavailable"
        )
    if (
        scan["failed"]
        or scan["not_scanned"]
        or (content_search and scan["not_in_storage"])
    ):
        result["partial"] = True
        result["partial_reason"] = (
            "Some scene configs could not be read within the bounded scan; config metadata and content matches are not exhaustive."
        )
    return result
