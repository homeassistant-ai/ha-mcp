"""Translate native recorder error codes without duplicating its request schemas."""

from ..errors import ErrorCode, create_error_response
from .helpers import raise_tool_error


def raise_recorder_ws_failure(
    kind: str,
    error_msg: str,
    entity_id_list: list[str],
    suggestions: list[str],
    error_code: str | None = None,
) -> None:
    """Classify a recorder reply. Transport failures already raise in the client."""
    if error_code == "invalid_format":
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Home Assistant rejected the {kind} parameters: {error_msg}",
                context={"entity_ids": entity_id_list, "ha_error_code": error_code},
                suggestions=[
                    "Correct the parameter named in Home Assistant's validation message",
                    "With the HA-MCP component installed and supporting schema discovery, use include_schema=True on a valid read to inspect the running Core schema",
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
