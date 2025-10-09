"""
Home Assistant MCP Server

A Model Context Protocol server that provides complete control over Home Assistant
through REST API and WebSocket integration with 20+ enhanced tools.
"""

__version__ = "2.3.1"
__author__ = "Julien"
__license__ = "MIT"

from .client.rest_client import HomeAssistantClient
from .config import Settings
from .server import HomeAssistantSmartMCPServer

__all__ = [
    "Settings",
    "HomeAssistantClient",
    "HomeAssistantSmartMCPServer",
]
