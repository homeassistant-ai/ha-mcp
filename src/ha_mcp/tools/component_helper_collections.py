"""Simple-helper schemas, reads and writes through the component's collection commands.

Every function returns ``None`` when the component cannot serve the request
and the caller should use Core's WebSocket commands instead. A write falls back
only when it provably never ran: the capability is absent, the command was not
sent, Core answered ``unknown_command``, or the component found no collection.
"""

from __future__ import annotations

import logging
import time
import weakref
from typing import Any, NoReturn

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantCommandNotSent,
    HomeAssistantError,
)
from ..client.websocket_client import get_websocket_client
from ..errors import ErrorCode, create_error_response
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .config_helpers.listing import listed_items
from .helpers import raise_tool_error

logger = logging.getLogger(__name__)

WS_HELPER_SCHEMAS = "ha_mcp_tools/helper_schemas"
WS_HELPER_ITEM = "ha_mcp_tools/helper_item"
WS_HELPER_WRITE = "ha_mcp_tools/helper_write"
HELPER_CAPABILITIES = ("helper_schemas", "helper_item", "helper_write")

# Core's schemas change only with a Core upgrade, which restarts the WS session.
_SCHEMA_TTL_S = 300.0
_SCHEMA_CACHE: weakref.WeakKeyDictionary[Any, tuple[float, dict[str, Any]]] = (
    weakref.WeakKeyDictionary()
)


async def _component_ws(client: Any) -> Any | None:
    """The client's WS connection when the component serves every helper command."""
    if not getattr(client, "base_url", None) or not getattr(client, "token", None):
        return None
    caps = await get_component_caps(client)
    if not all(component_supports(caps, name) for name in HELPER_CAPABILITIES):
        return None
    return await get_websocket_client(
        url=client.base_url,
        token=client.token,
        verify_ssl=getattr(client, "verify_ssl", None),
    )


async def _read(client: Any, command: str, **kwargs: Any) -> dict[str, Any] | None:
    """Send a read command; any failure means "use the legacy path"."""
    try:
        ws = await _component_ws(client)
        if ws is None:
            return None
        raw = await ws.send_command(command, **kwargs)
    except Exception as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        logger.warning(
            "%s failed; using Core's commands: %r", command, exc, exc_info=True
        )
        return None
    result = raw.get("result") if isinstance(raw, dict) else None
    return result if isinstance(result, dict) else None


async def fetch_helper_schemas(client: Any) -> dict[str, Any] | None:
    """Core's ``{helper_type: {"create": [...], "update": [...]}}`` field lists."""
    cached = _SCHEMA_CACHE.get(client)
    if cached is not None and time.monotonic() - cached[0] < _SCHEMA_TTL_S:
        return cached[1]
    result = await _read(client, WS_HELPER_SCHEMAS)
    types = result.get("types") if result else None
    if not isinstance(types, dict):
        return None
    _SCHEMA_CACHE[client] = (time.monotonic(), types)
    return types


async def read_helper_item(
    client: Any,
    helper_type: str,
    *,
    entity_id: str | None = None,
    item_id: str | None = None,
) -> dict[str, Any] | None:
    """The stored item and its id; ``None`` lets the legacy path read (and report)."""
    target = {"item_id": item_id} if item_id is not None else {"entity_id": entity_id}
    result = await _read(client, WS_HELPER_ITEM, helper_type=helper_type, **target)
    if not result or result.get("success") is not True:
        return None
    return result


async def read_stored_helper_item(
    client: Any,
    helper_type: str,
    helper_id: str,
    entity_id: str,
    name: str | None = None,
) -> dict[str, Any] | None:
    """The stored item ``helper_id`` names, as ``stored_helper_target``
    resolves it: a bare id is a storage id, and only ``<type>.<name>`` is read
    as the entity_id in the registry.

    A bare id the component does not find is not stored. A failed read returns
    ``None`` (the legacy path resolves again) instead.
    """
    result = await _read(
        client, WS_HELPER_ITEM, helper_type=helper_type, item_id=helper_id
    )
    if result is None:
        return None
    if result.get("success") is True:
        return result
    error = result.get("error")
    if not (isinstance(error, dict) and error.get("code") == "not_found"):
        return None
    if not helper_id.startswith(f"{helper_type}."):
        raise_not_stored(helper_type, helper_id, name)
    return await read_helper_item(client, helper_type, entity_id=entity_id)


def _raise_outcome_unknown(
    helper_type: str, action: str, exc: BaseException
) -> NoReturn:
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            f"The {helper_type} {action} was sent but its outcome is unknown: {exc}",
            context={"helper_type": helper_type, "write_outcome_unknown": True},
            suggestions=[
                "Check with ha_config_list_helpers before retrying, so a create "
                "is not repeated"
            ],
        )
    )


def _write_outcome(
    result: Any, helper_type: str, action: str, error_context: dict[str, Any]
) -> dict[str, Any] | None:
    """A success result, ``None`` when nothing ran, else the structured error."""
    if not isinstance(result, dict):
        _raise_outcome_unknown(helper_type, action, ValueError("malformed reply"))
    if result.get("success") is True:
        return result
    error = result.get("error") or {}
    if error.get("code") == "unavailable":
        return None
    code = (
        ErrorCode.CONFIG_NOT_FOUND
        if error.get("code") == "not_found"
        else ErrorCode.VALIDATION_INVALID_PARAMETER
    )
    raise_tool_error(
        create_error_response(
            code,
            f"Failed to {action} {helper_type}: {error.get('message', 'unknown error')}",
            context=error_context,
        )
    )
    return None  # py/mixed-returns: unreachable, raise_tool_error raises


async def write_helper_item(
    client: Any,
    helper_type: str,
    action: str,
    data: dict[str, Any],
    *,
    item_id: str | None = None,
    registry: dict[str, Any] | None = None,
    error_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Create/update a stored item plus its registry fields in one in-process call."""
    try:
        ws = await _component_ws(client)
    except Exception:
        logger.warning("Component helper_write unavailable", exc_info=True)
        return None
    if ws is None:
        return None
    kwargs: dict[str, Any] = {
        "helper_type": helper_type,
        "action": action,
        "data": data,
    }
    if item_id is not None:
        kwargs["item_id"] = item_id
    if registry:
        kwargs["registry"] = registry
    try:
        raw = await ws.send_command(WS_HELPER_WRITE, **kwargs)
    except HomeAssistantCommandNotSent:
        return None
    except Exception as exc:  # noqa: BLE001 - ends in a tool error
        # Cancellation propagates; only a definitive unknown_command falls back.
        if is_unknown_command(exc):
            invalidate_caps(client)
            return None
        _raise_outcome_unknown(helper_type, action, exc)
    return _write_outcome(
        raw.get("result") if isinstance(raw, dict) else None,
        helper_type,
        action,
        error_context or {"helper_type": helper_type},
    )


def collection_payload(helper_type: str, message: dict[str, Any]) -> dict[str, Any]:
    """A ``{type}/create|update`` WS message's item fields.

    ``<type>_id`` addresses the item on update; on a tag create it is a field.
    """
    drop = {"type"}
    if str(message.get("type", "")).endswith("/update"):
        drop.add(f"{helper_type}_id")
    return {key: value for key, value in message.items() if key not in drop}


def native_result(
    result: dict[str, Any],
) -> tuple[dict[str, Any], str | None, list[str]]:
    """Split a component write into (helper data, entity_id, warnings).

    The entity_id is the one the component read from the registry, None when
    the helper has none: ``<type>.<id>`` can be another helper's entity.
    """
    item = result.get("item") or {}
    data = {**item, **(result.get("registry_applied") or {})}
    return data, result.get("entity_id"), list(result.get("warnings") or [])


async def _platform_entities(client: Any, platform: str) -> list[dict[str, Any]]:
    """The platform's registry entries; a failed read raises, never reads as none."""
    listed = await client.send_websocket_message(
        {"type": "config/entity_registry/list"}
    )
    if not listed.get("success"):
        raise HomeAssistantAPIError(
            f"entity registry read failed: {listed.get('error', 'Unknown error')}"
        )
    return [e for e in listed.get("result") or [] if e.get("platform") == platform]


async def registry_entity_id(
    client: Any, platform: str, item_id: str | None
) -> str | None:
    """The entity_id of a stored item's entity: the registry entry on ``platform``
    whose unique_id is the item's ID. A tag's, zone's or person's entity_id
    follows its name, and a renamed entity keeps its unique_id, so only the
    registry knows it."""
    if not item_id:
        return None
    return next(
        (
            e.get("entity_id")
            for e in await _platform_entities(client, platform)
            if e.get("unique_id") == item_id
        ),
        None,
    )


async def stored_helper_target(
    client: Any, helper_type: str, target: str, name: str | None = None
) -> tuple[str, str | None, str | None] | None:
    """The stored helper whose storage id ``target`` is: that id, its entity_id
    and why it has none; ``None`` leaves an entity_id (``<type>.<name>``) to
    the entity registry lookup.

    ``ha_config_set_helper`` and ``ha_remove_helpers_integrations`` resolve a
    target as the backups' ``backup_helper_target.stored_target`` does: a bare
    id is a storage id and never the name of an entity, since a renamed entity
    can carry another helper's id as its name. A bare id no stored helper has
    is an error, so a repeated removal deletes nothing. The write goes by the
    storage id, so a failed registry read only leaves the entity unnamed. A
    failed list read is an error rather than reading as no stored helper.
    """
    listed = await client.send_websocket_message({"type": f"{helper_type}/list"})
    if not listed.get("success"):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to fetch {helper_type} config list: "
                f"{listed.get('error', 'Unknown error')}",
                context={"helper_type": helper_type, "target": target},
            )
        )
    stored = listed_items(listed.get("result"))
    if not any(str(item.get("id")) == target for item in stored):
        if target.startswith(f"{helper_type}."):
            return None
        raise_not_stored(helper_type, target, name)
    try:
        entity = await registry_entity_id(client, helper_type, target)
    except (HomeAssistantError, OSError, TimeoutError) as err:
        return target, None, f"the entity registry could not be read ({err})"
    reason = None if entity else f"the {helper_type} has no entity in the registry"
    return target, entity, reason


def raise_not_stored(
    helper_type: str, target: str, name: str | None = None
) -> NoReturn:
    """A bare id names a stored item only, never an entity by its name."""
    suggestions = [
        f"Find its id or entity_id with "
        f"ha_config_list_helpers(helper_type='{helper_type}')"
    ]
    if name:
        suggestions.append(
            f"If you meant to create a new helper named {name!r}, "
            "omit helper_id (or pass action='create')"
        )
    raise_tool_error(
        create_error_response(
            ErrorCode.ENTITY_NOT_FOUND,
            f"No stored {helper_type} has the id '{target}', and it is no "
            f"{helper_type} entity_id. An id without '{helper_type}.' is a "
            "storage id; name a helper by its entity with the full entity_id.",
            context={"helper_type": helper_type, "target": target},
            suggestions=suggestions,
        )
    )


def removal_reply(
    response: dict[str, Any], entity: str | None, reason: str | None
) -> dict[str, Any]:
    """A removal's reply without a guessed entity, saying why it has none."""
    if entity is None:
        response["message"] = (
            f"Successfully deleted {response['helper_type']}: {response['target']}."
        )
        response.setdefault("warnings", []).append(
            f"{response['helper_type']} {response['target']} was deleted, but "
            f"{reason}, so its entity is not named and the removal was not waited on."
        )
    return response
