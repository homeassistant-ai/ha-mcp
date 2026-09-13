"""Client mixins for FastMCP."""

from ha_mcp._vendor.fastmcp.client.mixins.prompts import ClientPromptsMixin
from ha_mcp._vendor.fastmcp.client.mixins.resources import ClientResourcesMixin
from ha_mcp._vendor.fastmcp.client.mixins.tools import ClientToolsMixin

__all__ = [
    "ClientPromptsMixin",
    "ClientResourcesMixin",
    "ClientToolsMixin",
]
