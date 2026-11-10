"""Entity registry reads shared by ``ha_get_entity``'s list and unique_id modes."""

import asyncio
import logging
from typing import Any

from ..errors import ErrorCode, create_error_response
from .helpers import raise_tool_error

logger = logging.getLogger(__name__)

# Bounds the per-frame size of a bulk config/entity_registry/get_entries call
# (extended entries, ~1KB each) so a large id list can't produce an over-cap
# WebSocket frame. Matches the chunk size smart_search uses for the same
# command. Typical bulk calls fit in a single chunk (one WS message).
_GET_ENTRIES_CHUNK_SIZE = 500

# The per-id error of an id get_entries returns no entry for.
_NOT_IN_REGISTRY = "Entity not found"


def _extract_ws_error(result: dict[str, Any]) -> str:
    """Pull a user-readable message out of a failed WebSocket response.

    Falls back to a static placeholder + warning log when HA returns an
    empty or malformed error envelope, so the user-facing message never
    degrades to literal "{}".
    """
    error = result.get("error")
    if isinstance(error, dict):
        msg = error.get("message")
        if isinstance(msg, str) and msg:
            return msg
    elif isinstance(error, str) and error:
        return error
    logger.warning("HA WS response had no usable error detail: %r", result)
    return "no error detail returned by Home Assistant"


async def get_entries_raw(
    client: Any, entity_ids: list[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Bulk-fetch registry entries via chunked config/entity_registry/get_entries.

    Returns ``(raw_entries, errors)``: ``raw_entries`` maps a found
    entity_id to HA's raw extended registry dict; ``errors`` maps a failed
    entity_id to a message. A ``null`` in HA's entries map (id not in the
    registry) and a chunk-level WS failure both land in ``errors`` — this
    reproduces the per-id not-found contract of the old one-get-per-id
    fan-out, where every id got its own error.
    """
    raw: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    if not entity_ids:
        return raw, errors

    chunks = [
        entity_ids[i : i + _GET_ENTRIES_CHUNK_SIZE]
        for i in range(0, len(entity_ids), _GET_ENTRIES_CHUNK_SIZE)
    ]
    responses = await asyncio.gather(
        *(
            client.send_websocket_message(
                {
                    "type": "config/entity_registry/get_entries",
                    "entity_ids": chunk,
                }
            )
            for chunk in chunks
        ),
        return_exceptions=True,
    )
    for chunk, resp in zip(chunks, responses, strict=True):
        if isinstance(resp, BaseException) and not isinstance(resp, Exception):
            # Never swallow cancellation/shutdown into per-entity errors.
            raise resp
        if isinstance(resp, dict) and resp.get("success"):
            result_map = resp.get("result") or {}
            for eid in chunk:
                # Core lowercases the ids it is sent and keys its reply so.
                entry = result_map.get(eid.lower())
                if entry is None:
                    errors[eid] = _NOT_IN_REGISTRY
                else:
                    raw[eid] = entry
            continue
        # Whole-chunk failure (WS raised, or success=False): every id in
        # the chunk failed, mirroring the old per-id fan-out behaviour.
        msg = _extract_ws_error(resp) if isinstance(resp, dict) else str(resp)
        for eid in chunk:
            errors[eid] = msg
    return raw, errors


async def resolve_by_unique_id(
    client: Any,
    unique_id: str,
    domain: str | None,
    platform: str | None,
) -> dict[str, Any]:
    """Resolve a stable unique_id to its entity_id(s) and their full entries.

    The registry's unique key is (domain, platform, unique_id), so the same
    unique_id can appear under multiple platforms — every match is returned
    with a ``matches`` count so callers can disambiguate. ``domain`` /
    ``platform`` narrow the match set when provided.
    """
    list_resp = await client.send_websocket_message(
        {"type": "config/entity_registry/list"}
    )
    if not list_resp.get("success"):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to list entity registry: {_extract_ws_error(list_resp)}",
                context={"unique_id": unique_id},
                suggestions=["Check Home Assistant connection and retry"],
            )
        )

    matches = [
        entry.get("entity_id") or ""
        for entry in list_resp.get("result") or []
        if entry.get("unique_id") == unique_id
        and (domain is None or (entry.get("entity_id") or "").split(".")[0] == domain)
        and (platform is None or entry.get("platform") == platform)
    ]

    if not matches:
        filters = {"unique_id": unique_id}
        if domain is not None:
            filters["domain"] = domain
        if platform is not None:
            filters["platform"] = platform
        raise_tool_error(
            create_error_response(
                ErrorCode.ENTITY_NOT_FOUND,
                f"No entity found with unique_id '{unique_id}'"
                + (
                    f" (domain={domain}, platform={platform})"
                    if domain is not None or platform is not None
                    else ""
                ),
                context=filters,
                suggestions=[
                    "unique_id must match exactly — it is the integration's "
                    + "internal id, not the entity_id",
                    "Drop the domain/platform filters if you set them",
                    "Use ha_search() to browse entities and their platforms",
                ],
            )
        )

    # The list rows lack the extended fields (e.g. aliases, capabilities).
    raw_map, errors = await get_entries_raw(client, matches)
    if errors:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to read the entries matching unique_id '{unique_id}'",
                context={"unique_id": unique_id, "errors": errors},
                suggestions=["Check Home Assistant connection and retry"],
            )
        )
    entries = [raw_map[eid] for eid in matches]
    response: dict[str, Any] = {
        "success": True,
        "unique_id": unique_id,
        "matches": len(entries),
        "entity_entries": entries,
    }
    if domain is not None:
        response["domain"] = domain
    if platform is not None:
        response["platform"] = platform
    return response
