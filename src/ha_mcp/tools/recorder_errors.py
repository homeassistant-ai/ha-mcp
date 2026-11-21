"""A recorder command Core refused, passed on with Core's own code and message."""

from typing import Any, Literal, NoReturn

from ..errors import ErrorCode
from .helpers import raise_core_refusal, ws_failure_code

_REJECTED_REQUEST_SUGGESTIONS = [
    "Correct the parameter named in Home Assistant's validation message",
    "With the HA-MCP component installed, include_schema=True on a read Core accepts shows the schema of the command that read uses",
]


def raise_recorder_refusal(
    kind: Literal["history", "statistics"],
    response: dict[str, Any],
    statistic_or_entity_ids: list[str],
) -> NoReturn:
    """Raise Core's refusal of a ``kind`` (history, statistics) request."""
    rejected = ws_failure_code(response) is ErrorCode.VALIDATION_INVALID_PARAMETER
    raise_core_refusal(
        f"Home Assistant refused the {kind} request",
        response,
        {"entity_ids": statistic_or_entity_ids},
        _REJECTED_REQUEST_SUGGESTIONS if rejected else None,
    )
