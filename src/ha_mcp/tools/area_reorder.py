"""Reordering for ``ha_set_area_or_floor``."""

from typing import Any

from pydantic.fields import FieldInfo

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


async def reorder_area_or_floor(
    client: Any,
    kind: str,
    order: str | list[str],
    plain: dict[str, Any],
    clearable: dict[str, Any],
) -> dict[str, Any]:
    """Send Core's reorder command; Core checks the list is complete."""
    context = {"operation": "reorder", "kind": kind}
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
        result = await client.send_websocket_message(
            {"type": f"config/{kind}_registry/reorder", f"{kind}_ids": ids}
        )
    except Exception as e:  # noqa: BLE001
        exception_to_structured_error(e, context=context)
    if not result.get("success"):
        raise_core_refusal(f"Failed to reorder {kind}s", result, context=context)
    return {
        "success": True,
        "kind": kind,
        "order": ids,
        "message": f"Successfully reordered {kind}s",
    }
