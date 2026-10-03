"""
Home Assistant MCP Server

A Model Context Protocol server that provides complete control over Home Assistant
through REST API and WebSocket integration.
"""

# Keep this before any lazy export can reach fastmcp, which configures rich
# logging at import time and dies on a rich older than 13.9.4. See #2186.
from importlib import import_module
from typing import TYPE_CHECKING, Any

from . import _rich_compat as _rich_compat
from ._version import get_version

__version__ = get_version()
__author__ = "Julien"
__license__ = "MIT"

__all__ = [
    "Settings",
    "HomeAssistantClient",
    "HomeAssistantSmartMCPServer",
    "HomeAssistantOAuthProvider",
    # Error handling exports
    "ErrorCode",
    "create_error_response",
    "create_connection_error",
    "create_auth_error",
    "create_entity_not_found_error",
    "create_entity_unavailable_after_dispatch_error",
    "create_service_error",
    "create_validation_error",
    "create_config_error",
    "create_timeout_error",
    "is_error_response",
    "get_error_code",
    "get_error_message",
]

_EXPORT_MODULES = {
    "Settings": ".config",
    "HomeAssistantClient": ".client.rest_client",
    "HomeAssistantSmartMCPServer": ".server",
    "HomeAssistantOAuthProvider": ".auth",
    "ErrorCode": ".errors",
    "create_error_response": ".errors",
    "create_connection_error": ".errors",
    "create_auth_error": ".errors",
    "create_entity_not_found_error": ".errors",
    "create_entity_unavailable_after_dispatch_error": ".errors",
    "create_service_error": ".errors",
    "create_validation_error": ".errors",
    "create_config_error": ".errors",
    "create_timeout_error": ".errors",
    "is_error_response": ".errors",
    "get_error_code": ".errors",
    "get_error_message": ".errors",
}


def __getattr__(name: str) -> Any:
    """Load public exports on demand to keep package startup lightweight."""
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


if TYPE_CHECKING:
    from .auth import HomeAssistantOAuthProvider as HomeAssistantOAuthProvider
    from .client.rest_client import HomeAssistantClient as HomeAssistantClient
    from .config import Settings as Settings
    from .errors import (
        ErrorCode as ErrorCode,
    )
    from .errors import (
        create_auth_error as create_auth_error,
    )
    from .errors import (
        create_config_error as create_config_error,
    )
    from .errors import (
        create_connection_error as create_connection_error,
    )
    from .errors import (
        create_entity_not_found_error as create_entity_not_found_error,
    )
    from .errors import (
        create_entity_unavailable_after_dispatch_error as create_entity_unavailable_after_dispatch_error,
    )
    from .errors import (
        create_error_response as create_error_response,
    )
    from .errors import (
        create_service_error as create_service_error,
    )
    from .errors import (
        create_timeout_error as create_timeout_error,
    )
    from .errors import (
        create_validation_error as create_validation_error,
    )
    from .errors import (
        get_error_code as get_error_code,
    )
    from .errors import (
        get_error_message as get_error_message,
    )
    from .errors import (
        is_error_response as is_error_response,
    )
    from .server import HomeAssistantSmartMCPServer as HomeAssistantSmartMCPServer
