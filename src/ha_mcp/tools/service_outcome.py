"""Shared presentation for locally established uncertain service outcomes."""

from typing import Any

from ..client.rest_client import HomeAssistantCommandError
from ..client.service_errors import (
    HomeAssistantServiceAPIOutcomeUnknown,
    HomeAssistantServiceAuthOutcomeUnknown,
    HomeAssistantWebSocketServiceError,
)
from ..errors import ErrorCode, create_error_response


def create_service_outcome_unknown_error(
    error: Exception, *, context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Retain diagnostics while requiring readback before another service attempt."""
    details = dict(context or {})
    domain, service = details.get("domain"), details.get("service")
    label = f"{domain}.{service}" if domain and service else "the service call"
    details.update(
        write_outcome_unknown=True,
        ha_error_code=(
            error.code
            if isinstance(
                error,
                (
                    HomeAssistantCommandError,
                    HomeAssistantWebSocketServiceError,
                    HomeAssistantServiceAuthOutcomeUnknown,
                ),
            )
            else None
        ),
    )
    if (
        isinstance(
            error,
            (
                HomeAssistantServiceAuthOutcomeUnknown,
                HomeAssistantServiceAPIOutcomeUnknown,
            ),
        )
        and error.status_code is not None
    ):
        details["http_status"] = error.status_code
    response = create_error_response(
        ErrorCode.SERVICE_CALL_FAILED,
        f"The outcome of {label} is unknown; it may have taken effect.",
        details=str(error),
        context=details,
        suggestions=[
            "Check the target state and Home Assistant logs before retrying; "
            "the service may already have acted."
        ],
    )
    return response
