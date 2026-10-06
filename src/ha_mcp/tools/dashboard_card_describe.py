"""Describe Lovelace card types from Home Assistant's own frontend (issue #2632).

The ``ha_mcp_tools`` component reads each card editor's form from the frontend
Home Assistant serves; this module compacts it the way helper ``describe`` does.
"""

from __future__ import annotations

from typing import Any

from ..client.websocket_client import get_websocket_client
from ..errors import ErrorCode, create_error_response
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .config_helpers.describe import compact_field
from .helpers import exception_to_structured_error, raise_tool_error

WS_DASHBOARD_CARDS = "ha_mcp_tools/dashboard_cards"


def _flatten(schema: list[Any]) -> list[dict[str, Any]]:
    """Drop the form's layout-only nodes (grids, dividers), keep its sections."""
    fields: list[dict[str, Any]] = []
    for node in schema:
        if not isinstance(node, dict) or node.get("type") == "divider":
            continue
        children = node.get("schema") if isinstance(node.get("schema"), list) else None
        if (
            children is not None
            and node.get("type") == "expandable"
            and node.get("name")
        ):
            fields.append({**node, "schema": _flatten(children)})
        elif children is not None:
            fields.extend(_flatten(children))
        elif node.get("name"):
            fields.append(node)
    return fields


async def describe_card_response(client: Any, card_type: str | None) -> dict[str, Any]:
    """The card type list, or one card type's fields as its UI editor defines them."""
    card_type = card_type or None
    caps = await get_component_caps(client)
    if not component_supports(caps, "dashboard_cards"):
        raise_tool_error(
            create_error_response(
                ErrorCode.COMPONENT_NOT_INSTALLED,
                "Describing card types needs the ha_mcp_tools custom component "
                "with dashboard card support.",
                suggestions=[
                    "Install or update the HA-MCP Custom Component through HACS",
                    "Restart Home Assistant after the update completes",
                ],
                context={"action": "describe", "card_type": card_type},
            )
        )
    try:
        ws = await get_websocket_client(
            url=client.base_url,
            token=client.token,
            verify_ssl=getattr(client, "verify_ssl", None),
        )
        kwargs = {"card_type": card_type} if card_type else {}
        raw = await ws.send_command(WS_DASHBOARD_CARDS, **kwargs)
    except Exception as exc:  # noqa: BLE001
        if is_unknown_command(exc):
            invalidate_caps(client)
        exception_to_structured_error(
            exc, context={"action": "describe", "card_type": card_type}
        )
    result = raw.get("result") if isinstance(raw, dict) else None
    if not isinstance(result, dict):
        result = {}
    if result.get("success") is not True:
        error = result.get("error")
        if error in ("custom_cards_loading", "custom_card_not_inspected"):
            status = result.get("custom_status", {})
            loading = error == "custom_cards_loading"
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Custom card inspection is still loading; retry shortly."
                    if loading
                    else "This custom card could not be inspected from dashboard resources.",
                    suggestions=["Retry after resource loading finishes"]
                    if loading
                    else [
                        "Check the resource inspection status and type spelling",
                        "Cards loaded through extra JavaScript may work in the browser without appearing in dashboard resources",
                    ],
                    context={
                        "action": "describe",
                        "card_type": card_type,
                        "custom_status": status,
                    },
                )
            )
        if error == "unknown_card_type":
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"'{card_type}' is not a known card type.",
                    suggestions=[
                        "Use one of: " + ", ".join(result.get("card_types", [])),
                    ],
                    context={"action": "describe", "card_type": card_type},
                )
            )
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Home Assistant's card definitions are unavailable.",
                details=str(error),
                context={"action": "describe", "card_type": card_type},
            )
        )
    if card_type is None:
        return {
            "success": True,
            "card_types": result.get("card_types", []),
            **(
                {"custom_status": result["custom_status"]}
                if "custom_status" in result
                else {}
            ),
        }
    fields = result.get("fields")
    response: dict[str, Any] = {
        "success": True,
        "card_type": card_type,
        "name": result.get("name"),
        "description": result.get("description"),
        "fields": [compact_field(f) for f in _flatten(fields)]
        if isinstance(fields, list)
        else None,
    }
    if response["fields"] is None:
        response["note"] = (
            "The custom card's editor fields could not be inspected; "
            "the card may still work in the browser."
            if card_type.startswith("custom:")
            else "Home Assistant's editor for this card has no field form; stack, "
            "grid and conditional cards nest other cards under cards or card."
        )
    return response
