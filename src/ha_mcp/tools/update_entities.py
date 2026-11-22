"""The update entities ``ha_manage_updates`` reads, one flat row per entity.

The component's ``update_entities`` bridge names each entity's features from
Core's ``UpdateEntityFeature``. Without it the rows come from Core's states and
entity registry, with ``supported_features`` left undecoded.
"""

import logging
from typing import Any, NoReturn

from .._vendor.fastmcp.exceptions import ToolError
from .._version import is_running_in_addon
from ..client.exceptions import HomeAssistantConnectionError
from ..errors import ErrorCode, create_error_response
from .component_api import (
    COMPONENT_INSTALL_HINT,
    UNKNOWN_COMMAND_CODE,
    component_serves,
    invalidate_caps,
)
from .helpers import get_entity_state_or_none, raise_tool_error
from .update_categories import categorize_update

logger = logging.getLogger(__name__)

_CAPABILITY = "update_entities"
_NO_FEATURES_WARNING = (
    "Each row carries Home Assistant's supported_features number; decoding it "
    "into feature names (which updates can be installed, backed up or "
    "installed at a chosen version) needs the HA-MCP component, so features "
    f"and can_install are None. {COMPONENT_INSTALL_HINT}"
)
_NO_CATEGORIES_WARNING = (
    "Home Assistant's entity registry could not be read, so the integration "
    "behind each update and its category could not be determined."
)


def _raise_not_found(entity_id: str) -> NoReturn:
    raise_tool_error(
        create_error_response(
            ErrorCode.ENTITY_NOT_FOUND,
            f"Update entity not found: {entity_id}",
            context={"entity_id": entity_id},
            suggestions=[
                "Use ha_manage_updates() without entity_ids to see all "
                "available updates"
            ],
        )
    )


def _row(
    state: dict[str, Any],
    platform: str | None,
    features: list[str] | None,
    *,
    platform_known: bool = True,
) -> dict[str, Any]:
    attributes: dict[str, Any] = state.get("attributes") or {}
    return {
        **attributes,
        "entity_id": state["entity_id"],
        "state": state["state"],
        "update_available": state["state"] == "on",
        "platform": platform,
        "category": categorize_update(platform, attributes) if platform_known else None,
        "features": features,
        "can_install": None
        if features is None
        else "install" in features and attributes.get("in_progress") is False,
    }


def _bridge_row(row: Any) -> dict[str, Any] | None:
    if not (
        isinstance(row, dict)
        and isinstance(row.get("entity_id"), str)
        and isinstance(row.get("state"), str)
        and isinstance(row.get("attributes"), dict)
        and isinstance(row.get("features"), list)
    ):
        return None
    return _row(row, row.get("platform"), row["features"])


async def _bridge_rows(
    client: Any, entity_id: str | None
) -> list[dict[str, Any]] | None:
    """The bridge's rows; None when it did not answer usably."""
    message: dict[str, Any] = {"type": f"ha_mcp_tools/{_CAPABILITY}"}
    if entity_id is not None:
        message["entity_id"] = entity_id
    try:
        reply = await client.send_websocket_message(message)
    except HomeAssistantConnectionError as e:
        logger.debug("update_entities bridge gave no answer: %s", e)
        return None
    if not reply.get("success"):
        if reply.get("error_code") == UNKNOWN_COMMAND_CODE:
            invalidate_caps(client)
        return None
    updates = (reply.get("result") or {}).get("updates")
    if not isinstance(updates, list):
        return None
    rows = [_bridge_row(row) for row in updates]
    if None in rows:
        return None
    if entity_id is not None and not rows:
        _raise_not_found(entity_id)
    return [row for row in rows if row is not None]


async def _update_platforms(client: Any) -> dict[str, str] | None:
    """Each registered update entity's integration; None when the registry
    could not be read."""
    try:
        reply = await client.send_websocket_message(
            {"type": "config/entity_registry/list_for_display"}
        )
    except HomeAssistantConnectionError as e:
        logger.debug("Entity registry unreachable for update categories: %s", e)
        return None
    if not reply.get("success"):
        return None
    return {
        row["ei"]: row["pl"]
        for row in (reply.get("result") or {}).get("entities", [])
        if row.get("ei", "").startswith("update.") and row.get("pl")
    }


async def _core_rows(
    client: Any, entity_id: str | None
) -> tuple[list[dict[str, Any]], list[str]]:
    if entity_id is None:
        states = [
            s
            for s in await client.get_states()
            if s.get("entity_id", "").startswith("update.")
        ]
    else:
        state = await get_entity_state_or_none(client, entity_id)
        if state is None:
            _raise_not_found(entity_id)
        states = [state]
    warnings = [_NO_FEATURES_WARNING]
    platforms = await _update_platforms(client)
    if platforms is None:
        warnings.append(_NO_CATEGORIES_WARNING)
    rows = [
        _row(
            s,
            (platforms or {}).get(s["entity_id"]),
            None,
            platform_known=platforms is not None,
        )
        for s in states
    ]
    return rows, warnings


async def read_update_rows(
    client: Any, entity_id: str | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """The update rows (every update entity, or ``entity_id`` alone) and the
    warnings that say what they could not tell.

    A row holds all of Core's attributes, then ``entity_id``, ``state``,
    ``update_available``, ``platform``, ``category``, ``features`` (the
    lowercase ``UpdateEntityFeature`` names, None without the component) and
    ``can_install`` (None without the component). An ``entity_id`` that names
    no update entity raises ENTITY_NOT_FOUND.
    """
    if await component_serves(client, _CAPABILITY):
        rows = await _bridge_rows(client, entity_id)
        if rows is not None:
            return rows, []
    return await _core_rows(client, entity_id)


async def mark_server_app(
    client: Any, rows: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The row of the app this server runs in, marked ``runs_this_server``.

    The hassio platform titles an app's update with the app's name, which
    Supervisor's ``/addons/self/info`` returns. None outside the app, when no
    row is an app's, or when Supervisor could not be read.
    """
    apps = [row for row in rows if row["category"] == "addons"]
    if not apps or not is_running_in_addon():
        return None
    from .tools_addons import _supervisor_api_call

    try:
        reply = await _supervisor_api_call(client, "/addons/self/info")
    except ToolError as e:
        logger.debug("This app's own info could not be read: %s", e)
        return None
    info = reply.get("result")
    name = info.get("name") if isinstance(info, dict) else None
    own = next((row for row in apps if name and row.get("title") == name), None)
    if own is not None:
        own["runs_this_server"] = True
    return own
