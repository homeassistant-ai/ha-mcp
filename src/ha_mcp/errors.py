"""
Structured error handling for Home Assistant MCP Server.

This module provides standardized error codes, error response models, and helper
functions for creating consistent, informative error responses across all MCP tools.

The structured error format enables AI agents to:
- Diagnose issues programmatically using error codes
- Provide helpful suggestions to users
- Understand the context and details of failures
"""

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """
    Standard error codes for Home Assistant MCP operations.

    Error codes are grouped by category:
    - CONNECTION_*: Network and connectivity issues
    - AUTH_*: Authentication and authorization issues
    - ENTITY_*: Entity-related issues
    - SERVICE_*: Service call issues
    - CONFIG_*: Configuration issues
    - VALIDATION_*: Input validation issues
    - TIMEOUT_*: Timeout issues
    - INTERNAL_*: Internal server errors
    """

    # Connection errors
    CONNECTION_FAILED = "CONNECTION_FAILED"
    CONNECTION_TIMEOUT = "CONNECTION_TIMEOUT"
    WEBSOCKET_DISCONNECTED = "WEBSOCKET_DISCONNECTED"
    WEBSOCKET_NOT_AUTHENTICATED = "WEBSOCKET_NOT_AUTHENTICATED"

    # Authentication errors
    AUTH_INVALID_TOKEN = "AUTH_INVALID_TOKEN"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    AUTH_INSUFFICIENT_PERMISSIONS = "AUTH_INSUFFICIENT_PERMISSIONS"

    # Entity errors
    ENTITY_NOT_FOUND = "ENTITY_NOT_FOUND"
    ENTITY_UNAVAILABLE = "ENTITY_UNAVAILABLE"
    ENTITY_INVALID_ID = "ENTITY_INVALID_ID"
    ENTITY_DOMAIN_MISMATCH = "ENTITY_DOMAIN_MISMATCH"

    # Service errors
    SERVICE_NOT_FOUND = "SERVICE_NOT_FOUND"
    SERVICE_INVALID_DOMAIN = "SERVICE_INVALID_DOMAIN"
    SERVICE_INVALID_ACTION = "SERVICE_INVALID_ACTION"
    SERVICE_CALL_FAILED = "SERVICE_CALL_FAILED"

    # Configuration errors
    CONFIG_NOT_FOUND = "CONFIG_NOT_FOUND"
    CONFIG_INVALID = "CONFIG_INVALID"
    CONFIG_MISSING_REQUIRED_FIELDS = "CONFIG_MISSING_REQUIRED_FIELDS"
    CONFIG_VALIDATION_FAILED = "CONFIG_VALIDATION_FAILED"

    # Validation errors
    VALIDATION_FAILED = "VALIDATION_FAILED"
    VALIDATION_INVALID_JSON = "VALIDATION_INVALID_JSON"
    VALIDATION_INVALID_PARAMETER = "VALIDATION_INVALID_PARAMETER"
    VALIDATION_MISSING_PARAMETER = "VALIDATION_MISSING_PARAMETER"

    # Timeout errors
    TIMEOUT_OPERATION = "TIMEOUT_OPERATION"
    TIMEOUT_WEBSOCKET = "TIMEOUT_WEBSOCKET"
    TIMEOUT_API_REQUEST = "TIMEOUT_API_REQUEST"

    # Internal errors
    INTERNAL_ERROR = "INTERNAL_ERROR"
    INTERNAL_UNEXPECTED = "INTERNAL_UNEXPECTED"

    # Resource errors
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    RESOURCE_ALREADY_EXISTS = "RESOURCE_ALREADY_EXISTS"
    RESOURCE_LOCKED = "RESOURCE_LOCKED"


# Default suggestions for common error codes
DEFAULT_SUGGESTIONS: dict[ErrorCode, list[str]] = {
    ErrorCode.CONNECTION_FAILED: [
        "Check if Home Assistant is running and accessible",
        "Verify the HOMEASSISTANT_URL environment variable is correct",
        "Check network connectivity to Home Assistant",
    ],
    ErrorCode.CONNECTION_TIMEOUT: [
        "Home Assistant may be overloaded or slow to respond",
        "Check network latency to Home Assistant",
        "Try increasing the timeout value if available",
    ],
    ErrorCode.WEBSOCKET_DISCONNECTED: [
        "WebSocket connection was lost",
        "Check Home Assistant logs for errors",
        "The operation may need to be retried",
    ],
    ErrorCode.WEBSOCKET_NOT_AUTHENTICATED: [
        "WebSocket connection is not authenticated",
        "Verify the access token is valid",
        "Try reconnecting to Home Assistant",
    ],
    ErrorCode.AUTH_INVALID_TOKEN: [
        "Verify the HOMEASSISTANT_TOKEN is correct",
        "Generate a new long-lived access token in Home Assistant",
        "Check token has not been revoked",
    ],
    ErrorCode.AUTH_EXPIRED: [
        "The access token has expired",
        "Generate a new long-lived access token in Home Assistant",
    ],
    ErrorCode.AUTH_INSUFFICIENT_PERMISSIONS: [
        "The access token does not have sufficient permissions",
        "Check token permissions in Home Assistant",
        "Create a new token with required permissions",
    ],
    ErrorCode.ENTITY_NOT_FOUND: [
        "Use ha_search_entities() to find correct entity ID",
        "Verify the entity exists in Home Assistant",
        "Check for typos in the entity ID",
    ],
    ErrorCode.ENTITY_UNAVAILABLE: [
        "The entity exists but is currently unavailable",
        "Check if the device is powered on and connected",
        "Check Home Assistant integration status",
    ],
    ErrorCode.ENTITY_INVALID_ID: [
        "Entity ID must be in format: domain.name",
        "Use ha_search_entities() to find valid entity IDs",
    ],
    ErrorCode.ENTITY_DOMAIN_MISMATCH: [
        "Cannot change entity to a different domain",
        "Entity domain must match the original domain",
    ],
    ErrorCode.SERVICE_NOT_FOUND: [
        "Use ha_get_domain_docs() to see available services",
        "Check the service name spelling",
        "Verify the domain supports this service",
    ],
    ErrorCode.SERVICE_INVALID_DOMAIN: [
        "Use ha_get_overview() to see available domains",
        "Check the domain name spelling",
    ],
    ErrorCode.SERVICE_INVALID_ACTION: [
        "Check available actions for this domain",
        "Common actions: turn_on, turn_off, toggle",
        "Use ha_get_domain_docs() for service documentation",
    ],
    ErrorCode.SERVICE_CALL_FAILED: [
        "Check the service parameters are correct",
        "Verify the target entity supports this service",
        "Check Home Assistant logs for detailed error",
    ],
    ErrorCode.CONFIG_NOT_FOUND: [
        "Verify the resource exists",
        "Use search tools to find the correct identifier",
    ],
    ErrorCode.CONFIG_INVALID: [
        "Review the configuration format",
        "Use ha_get_domain_docs() for configuration help",
    ],
    ErrorCode.CONFIG_MISSING_REQUIRED_FIELDS: [
        "Check documentation for required fields",
        "Ensure all required parameters are provided",
    ],
    ErrorCode.VALIDATION_INVALID_JSON: [
        "Ensure the parameter is valid JSON",
        "Check for syntax errors in JSON",
        "Use a JSON validator to verify the format",
    ],
    ErrorCode.VALIDATION_INVALID_PARAMETER: [
        "Check the parameter type and format",
        "Review the tool documentation for expected values",
    ],
    ErrorCode.TIMEOUT_OPERATION: [
        "The operation took too long to complete",
        "Home Assistant may be under heavy load",
        "Try the operation again",
    ],
    ErrorCode.INTERNAL_ERROR: [
        "An internal error occurred",
        "Check Home Assistant MCP server logs",
        "Report this issue if it persists",
    ],
}


def create_error_response(
    code: ErrorCode,
    message: str,
    details: str | None = None,
    suggestions: list[str] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Create a structured error response.

    Args:
        code: Error code from ErrorCode enum
        message: Human-readable error message
        details: Additional details about the error (optional)
        suggestions: List of suggestions to resolve the error (optional)
        context: Additional context data (e.g., entity_id, domain) (optional)

    Returns:
        Structured error response dictionary with success=False

    Example:
        >>> create_error_response(
        ...     ErrorCode.ENTITY_NOT_FOUND,
        ...     "Entity light.nonexistent not found",
        ...     details="No entity with this ID exists in Home Assistant",
        ...     context={"entity_id": "light.nonexistent"}
        ... )
        {
            "success": False,
            "error": {
                "code": "ENTITY_NOT_FOUND",
                "message": "Entity light.nonexistent not found",
                "details": "No entity with this ID exists in Home Assistant",
                "suggestion": "Use ha_search_entities() to find correct entity ID"
            },
            "entity_id": "light.nonexistent"
        }
    """
    # Use provided suggestions or fall back to defaults
    error_suggestions = suggestions if suggestions else DEFAULT_SUGGESTIONS.get(code, [])

    error_dict: dict[str, Any] = {
        "code": code.value,
        "message": message,
    }

    if details:
        error_dict["details"] = details

    if error_suggestions:
        # Include first suggestion as primary, all suggestions in list
        error_dict["suggestion"] = error_suggestions[0]
        if len(error_suggestions) > 1:
            error_dict["suggestions"] = error_suggestions

    response: dict[str, Any] = {
        "success": False,
        "error": error_dict,
    }

    # Add context fields at top level for easy access
    if context:
        response.update(context)

    return response


def create_connection_error(
    message: str,
    details: str | None = None,
    timeout: bool = False,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a connection error response."""
    code = ErrorCode.CONNECTION_TIMEOUT if timeout else ErrorCode.CONNECTION_FAILED
    return create_error_response(code, message, details, context=context)


def create_auth_error(
    message: str,
    details: str | None = None,
    expired: bool = False,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an authentication error response."""
    code = ErrorCode.AUTH_EXPIRED if expired else ErrorCode.AUTH_INVALID_TOKEN
    return create_error_response(code, message, details, context=context)


def create_entity_not_found_error(
    entity_id: str,
    details: str | None = None,
) -> dict[str, Any]:
    """Create an entity not found error response."""
    return create_error_response(
        ErrorCode.ENTITY_NOT_FOUND,
        f"Entity '{entity_id}' not found",
        details=details or f"No entity with ID '{entity_id}' exists in Home Assistant",
        context={"entity_id": entity_id},
    )


def create_service_error(
    domain: str,
    service: str,
    message: str,
    details: str | None = None,
    entity_id: str | None = None,
) -> dict[str, Any]:
    """Create a service call error response."""
    context: dict[str, Any] = {"domain": domain, "service": service}
    if entity_id:
        context["entity_id"] = entity_id

    return create_error_response(
        ErrorCode.SERVICE_CALL_FAILED,
        message,
        details=details,
        context=context,
    )


def create_validation_error(
    message: str,
    parameter: str | None = None,
    details: str | None = None,
    invalid_json: bool = False,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a validation error response."""
    code = ErrorCode.VALIDATION_INVALID_JSON if invalid_json else ErrorCode.VALIDATION_FAILED
    # Build context, prioritizing explicit context but adding parameter if provided
    final_context: dict[str, Any] = {}
    if context:
        final_context.update(context)
    if parameter:
        final_context["parameter"] = parameter
    return create_error_response(code, message, details, context=final_context if final_context else None)


def create_config_error(
    message: str,
    identifier: str | None = None,
    missing_fields: list[str] | None = None,
    details: str | None = None,
) -> dict[str, Any]:
    """Create a configuration error response."""
    if missing_fields:
        code = ErrorCode.CONFIG_MISSING_REQUIRED_FIELDS
        details = details or f"Missing required fields: {', '.join(missing_fields)}"
    else:
        code = ErrorCode.CONFIG_INVALID

    context: dict[str, Any] = {}
    if identifier:
        context["identifier"] = identifier
    if missing_fields:
        context["missing_fields"] = missing_fields

    return create_error_response(code, message, details, context=context or None)


def create_timeout_error(
    operation: str,
    timeout_seconds: float,
    details: str | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a timeout error response."""
    final_context: dict[str, Any] = {}
    if context:
        final_context.update(context)
    final_context["operation"] = operation
    final_context["timeout_seconds"] = timeout_seconds
    return create_error_response(
        ErrorCode.TIMEOUT_OPERATION,
        f"Operation '{operation}' timed out after {timeout_seconds}s",
        details=details,
        context=final_context,
    )


def create_resource_not_found_error(
    resource_type: str,
    identifier: str,
    details: str | None = None,
) -> dict[str, Any]:
    """Create a resource not found error response."""
    return create_error_response(
        ErrorCode.RESOURCE_NOT_FOUND,
        f"{resource_type} '{identifier}' not found",
        details=details,
        context={"resource_type": resource_type, "identifier": identifier},
    )


def is_error_response(response: dict[str, Any]) -> bool:
    """Check if a response is an error response."""
    return response.get("success") is False and "error" in response


def get_error_code(response: dict[str, Any]) -> str | None:
    """Extract the error code from an error response."""
    if is_error_response(response):
        error = response.get("error", {})
        if isinstance(error, dict):
            return error.get("code")
    return None


def get_error_message(response: dict[str, Any]) -> str | None:
    """Extract the error message from an error response."""
    if is_error_response(response):
        error = response.get("error", {})
        if isinstance(error, dict):
            return error.get("message")
        if isinstance(error, str):
            return error
    return None
