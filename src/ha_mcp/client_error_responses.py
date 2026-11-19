"""Build structured MCP error responses from Home Assistant client exceptions."""

from typing import Any

import httpx

from .client.exceptions import (
    HomeAssistantAdminRequiredError,
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from .client.rest_client import NON_ADMIN_TOKEN_WARNING
from .errors import (
    ErrorCode,
    create_auth_error,
    create_connection_error,
    create_entity_not_found_error,
    create_error_response,
    create_timeout_error,
    create_validation_error,
)


def _classify_api_status(
    error: HomeAssistantAPIError,
    error_msg: str,
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Classify HomeAssistantAPIError by HTTP status code."""
    match error.status_code:
        case 404:
            entity_id = context.get("entity_id") if context else None
            if entity_id:
                result = create_entity_not_found_error(entity_id, details=error_msg)
            else:
                result = create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND, error_msg, context=context
                )
        case 401 | 403:
            result = create_auth_error(error_msg, context=context)
        case 400:
            result = create_validation_error(error_msg, context=context)
        case _:
            result = create_error_response(
                ErrorCode.SERVICE_CALL_FAILED, error_msg, context=context
            )
    return result


def _request_timeout(
    error: Exception, context: dict[str, Any] | None
) -> dict[str, Any]:
    timing = context or {}
    return create_timeout_error(
        timing.get("operation") or timing.get("command") or "request",
        timing.get("timeout_seconds", 30),
        details=str(error),
        context=context,
    )


def _command_error_response(
    error: HomeAssistantCommandError, context: dict[str, Any] | None
) -> dict[str, Any]:
    """Translate a refusal Core sent, preserving native and caller context."""
    message = str(error)
    code = error.code
    if code:
        context = {**(context or {}), "ha_error_code": code}
    if code in {"invalid_format", "invalid_info", "service_validation_error"}:
        return create_validation_error(message, context=context)
    if code == "timeout":
        return _request_timeout(error, context)
    if (
        code == "not_found"
        and error.translation_domain == "homeassistant"
        and error.translation_key == "service_not_found"
    ):
        return create_error_response(
            ErrorCode.SERVICE_NOT_FOUND, message, context=context
        )
    if code in {"not_found", "config_not_found"}:
        if code == "not_found" and context and (entity_id := context.get("entity_id")):
            return {
                **context,
                **create_entity_not_found_error(entity_id, details=message),
            }
        return create_error_response(
            ErrorCode.RESOURCE_NOT_FOUND, message, context=context
        )
    if code == "unauthorized":
        return create_error_response(
            ErrorCode.AUTH_INSUFFICIENT_PERMISSIONS, message, context=context
        )
    return create_error_response(
        ErrorCode.SERVICE_CALL_FAILED, message, context=context
    )


def create_client_error_response(
    error: Exception, context: dict[str, Any] | None
) -> dict[str, Any]:
    """Build a tool error from the transport type or Home Assistant's response."""
    message = str(error)
    if isinstance(error, HomeAssistantConnectionError):
        if isinstance(error.__cause__, HomeAssistantAuthError):
            return create_client_error_response(error.__cause__, context)
        return create_connection_error(
            message, timeout=error.timed_out, context=context
        )
    if isinstance(error, HomeAssistantAdminRequiredError):
        result = create_error_response(
            ErrorCode.AUTH_INSUFFICIENT_PERMISSIONS,
            message,
            suggestions=[
                "Use a long-lived access token from an administrator's profile",
                "Or, on the in-process server, use a token issued in the "
                + "integration's forms from the login of a local-only administrator account",
            ],
            context=context,
        )
        result["warnings"] = [NON_ADMIN_TOKEN_WARNING]
        return result
    if isinstance(error, HomeAssistantAuthError):
        return create_auth_error(message, context=context)
    if isinstance(error, HomeAssistantAPIError):
        return _classify_api_status(error, message, context)
    if isinstance(error, HomeAssistantCommandError):
        return _command_error_response(error, context)
    if isinstance(error, (TimeoutError, HomeAssistantCommandTimeout)):
        return _request_timeout(error, context)
    if isinstance(error, (ConnectionError, httpx.NetworkError)):
        return create_connection_error(message, context=context)
    if isinstance(error, ValueError):
        return create_validation_error(message, context=context)
    return create_error_response(
        ErrorCode.INTERNAL_ERROR,
        "An unexpected error occurred",
        details=message,
        context=context,
    )
