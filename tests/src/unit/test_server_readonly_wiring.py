"""Test that the Read Only Mode guard is wired into the server.

Mirrors ``tests/src/unit/test_server_policy_wiring.py``: build a
``MagicMock`` stub exposing only the attributes the methods touch and
call the unbound methods directly, avoiding the full HA/FastMCP boot
path in ``HomeAssistantSmartMCPServer.__init__``.

Unlike PolicyMiddleware, the read-only guard is ALWAYS installed — it
consults the live ``read_only_mode`` flag per request, so installation
must not depend on the startup flag value.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from ha_mcp.read_only import ReadOnlyMiddleware, ReadOnlyToolsTransform
from ha_mcp.server import HomeAssistantSmartMCPServer


def _make_server_stub(*, read_only_mode: bool) -> MagicMock:
    stub = MagicMock()
    stub.settings = MagicMock(read_only_mode=read_only_mode)
    stub.mcp = MagicMock()
    return stub


def test_catalog_filter_installed_regardless_of_flag():
    for flag in (True, False):
        stub = _make_server_stub(read_only_mode=flag)
        HomeAssistantSmartMCPServer._apply_read_only_catalog_filter(stub)
        assert stub.mcp.add_transform.call_count == 1
        args, _kwargs = stub.mcp.add_transform.call_args
        assert isinstance(args[0], ReadOnlyToolsTransform)


def test_middleware_installed_regardless_of_flag():
    for flag in (True, False):
        stub = _make_server_stub(read_only_mode=flag)
        HomeAssistantSmartMCPServer._apply_read_only_middleware(stub)
        assert stub.mcp.add_middleware.call_count == 1
        args, _kwargs = stub.mcp.add_middleware.call_args
        assert isinstance(args[0], ReadOnlyMiddleware)
