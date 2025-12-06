"""
Home Assistant MCP Server

A Model Context Protocol server that provides complete control over Home Assistant
through REST API and WebSocket integration with 20+ enhanced tools.
"""

__version__ = "4.17.0"
__author__ = "Julien"
__license__ = "MIT"

from .client.rest_client import HomeAssistantClient
from .config import Settings
from .errors import (
    ErrorCode,
    create_auth_error,
    create_config_error,
    create_connection_error,
    create_entity_not_found_error,
    create_error_response,
    create_resource_not_found_error,
    create_service_error,
    create_timeout_error,
    create_validation_error,
    get_error_code,
    get_error_message,
    is_error_response,
)
from .server import HomeAssistantSmartMCPServer

__all__ = [
    "Settings",
    "HomeAssistantClient",
    "HomeAssistantSmartMCPServer",
    # Error handling exports
    "ErrorCode",
    "create_error_response",
    "create_connection_error",
    "create_auth_error",
    "create_entity_not_found_error",
    "create_service_error",
    "create_validation_error",
    "create_config_error",
    "create_timeout_error",
    "create_resource_not_found_error",
    "is_error_response",
    "get_error_code",
    "get_error_message",
]
