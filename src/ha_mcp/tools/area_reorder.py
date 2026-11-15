"""Reordering for ``ha_set_area_or_floor``."""

from typing import Any

from pydantic.fields import FieldInfo

from ..backup_registry_items import registry_rows
from ..errors import ErrorCode, create_error_response
from .coercion import UNSET, parse_string_list_param
from .helpers import (
    exception_to_structured_error,
    raise_core_refusal,
    raise_tool_error,
)


def _passed_params(plain: dict[str, Any], clearable: dict[str, Any]) -> list[str]:
    """List the names of the params the caller passed.

    A plain param counts unless it is None; a clearable one (omitted is ``UNSET``
    or a ``FieldInfo``) counts unless omitted, so an explicit null is a pass.
    """
    return [name for name, value in plain.items() if value is not None] + [
        name
        for name, value in clearable.items()
        if value is not UNSET and not isinstance(value, FieldInfo)
    ]


async def _order(client: Any, kind: str) -> list[str]:
    """The ids of the areas or floors, in the order Core lists them."""
    return [row[f"{kind}_id"] for row in await registry_rows(client, kind)]


async def reorder_area_or_floor(
    client: Any,
    kind: str,
    order: str | list[str],
    plain: dict[str, Any],
    clearable: dict[str, Any],
) -> dict[str, Any]:
    """Send Core's reorder command; Core checks the list is complete.

    No backup is taken of a reorder, so the order is read before the command
    and returned as ``previous_order``; ``order`` is read from Core after it,
    and is None when that read fails.
    """
    context: dict[str, Any] = {"operation": "reorder", "kind": kind}
    try:
        ids = parse_string_list_param(order, "order")
    except ValueError as e:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Invalid order parameter: {e}",
                context=context,
            )
        )
    if clashing := _passed_params(plain, clearable):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"order cannot be combined with {clashing}",
                context={**context, "invalid_parameters": clashing},
                suggestions=["Pass order alone, or drop it to create or update"],
            )
        )
    try:
        previous = await _order(client, kind)
        context["previous_order"] = previous
        result = await client.send_websocket_message(
            {"type": f"config/{kind}_registry/reorder", f"{kind}_ids": ids}
        )
    except Exception as e:  # noqa: BLE001
        exception_to_structured_error(e, context=context)
    if not result.get("success"):
        raise_core_refusal(f"Failed to reorder {kind}s", result, context=context)
    reply: dict[str, Any] = {
        "success": True,
        "kind": kind,
        "previous_order": previous,
        "order": None,
        "warnings": [
            "A reorder is not covered by the auto-backup: previous_order is the "
            "only record of the order it replaced. Pass it as order to go back."
        ],
        "message": f"Successfully reordered {kind}s",
    }
    try:
        reply["order"] = await _order(client, kind)
    except Exception as e:  # noqa: BLE001
        # Core applied the reorder; a failed read of it is not a failed write.
        reply["warnings"].append(
            f"The {kind}s were reordered, but the new order could not be read "
            f"back ({e}). Read it with ha_list_floors_areas()."
        )
    return reply
