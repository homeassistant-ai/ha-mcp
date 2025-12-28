"""
OAuth 2.1 authentication for Home Assistant MCP Server.

This module provides OAuth 2.1 authentication with Dynamic Client Registration (DCR)
and a consent form for collecting Home Assistant credentials.
"""

from .provider import HomeAssistantOAuthProvider

__all__ = ["HomeAssistantOAuthProvider"]
