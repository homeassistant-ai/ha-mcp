"""Translate native recorder error codes without duplicating its request schemas."""

from ..errors import ErrorCode, create_error_response
from .helpers import raise_tool_error
from .util_helpers import is_connection_error_message


def raise_recorder_ws_failure(
    kind: str,
    error_msg: str,
    entity_id_list: list[str],
    suggestions: list[str],
    error_code: str | None = None,
) -> None:
    """Raise the structured error for a failed recorder WS call.

    The pooled ``send_websocket_message`` collapses transport failures into
    ``{"success": False, "error": ...}`` — classify connection-shaped errors
    as CONNECTION_FAILED (retry/connectivity guidance) instead of presenting
    recorder-retention suggestions during an HA restart or WS outage.
    """
    if error_code == "invalid_format":
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Home Assistant rejected the {kind} parameters: {error_msg}",
                context={"entity_ids": entity_id_list, "ha_error_code": error_code},
                suggestions=[
                    "Correct the parameter named in Home Assistant's validation message",
                    "Use include_schema=True on a valid read to inspect the running Core schema",
                ],
            )
        )
    if is_connection_error_message(error_msg):
        raise_tool_error(
            create_error_response(
                ErrorCode.CONNECTION_FAILED,
                f"Failed to retrieve {kind}: {error_msg}",
                context={"entity_ids": entity_id_list},
                suggestions=[
                    "Home Assistant may be restarting or unreachable — retry shortly",
                    "Check the connection to Home Assistant",
                ],
            )
        )
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            f"Failed to retrieve {kind}: {error_msg}",
            context={"entity_ids": entity_id_list},
            suggestions=suggestions,
        )
    )
