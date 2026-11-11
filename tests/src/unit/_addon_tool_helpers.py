"""An add-on info reply and a tool-error parser shared by the add-on tool tests."""

from __future__ import annotations

import json

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError

# Standard mock return for a running addon with Ingress support
RUNNING_ADDON_INFO = {
    "success": True,
    "addon": {
        "name": "Test Addon",
        "slug": "test_addon",
        "ingress": True,
        "state": "started",
        "ingress_entry": "/api/hassio_ingress/abc123",
        "ip_address": "172.30.33.99",
        "ingress_port": 5000,
    },
}


def _parse_tool_error(exc_info: pytest.ExceptionInfo[ToolError]) -> dict:
    """Parse the JSON payload from a ToolError."""
    return json.loads(str(exc_info.value))
