"""Visibility enforce-mode middleware wiring (#2015).

Mirrors ``test_server_readonly_wiring.py``: MagicMock stubs + unbound methods,
plus a source-order pin on ``_initialize_server`` — FastMCP registration order
IS the inbound execution order (first added = outermost), and the enforcement
pair's placement is load-bearing: the inbound conceal/refuse gate must precede
the read-only guard and PolicyMiddleware so a hidden entity_id never reaches
the approval queue (or earns an approval-pending response that confirms
existence), and the outbound scan must be registered last (innermost) so it
sees raw tool output.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from ha_mcp.server import HomeAssistantSmartMCPServer
from ha_mcp.visibility.enforcement import (
    VisibilityInboundEnforcement,
    VisibilityOutboundEnforcement,
)


def _make_server_stub() -> MagicMock:
    stub = MagicMock()
    stub.mcp = MagicMock()
    return stub


def test_inbound_middleware_installed():
    stub = _make_server_stub()
    HomeAssistantSmartMCPServer._apply_visibility_inbound_middleware(stub)
    assert stub.mcp.add_middleware.call_count == 1
    args, _kwargs = stub.mcp.add_middleware.call_args
    assert isinstance(args[0], VisibilityInboundEnforcement)


def test_outbound_middleware_installed():
    stub = _make_server_stub()
    HomeAssistantSmartMCPServer._apply_visibility_outbound_middleware(stub)
    assert stub.mcp.add_middleware.call_count == 1
    args, _kwargs = stub.mcp.add_middleware.call_args
    assert isinstance(args[0], VisibilityOutboundEnforcement)


def test_initialize_order_inbound_before_policy_outbound_last():
    """Pin the PROPERTY (call order), not its source-text proxy.

    Running the real ``_initialize_server`` against a MagicMock stub records
    every ``self.*`` call — the ``_apply_*`` helpers resolve to stub
    attributes (no real registration), while the inline
    ``self.mcp.add_middleware(...)`` registrations execute for real against
    the stubbed ``mcp``. The stub's ``mock_calls`` therefore captures the
    full registration order, so ANY middleware-installing call appended
    after the outbound half — helper or inline — fails the last assertion.
    """
    stub = _make_server_stub()
    HomeAssistantSmartMCPServer._initialize_server(stub)
    names = [name for name, _args, _kwargs in stub.mock_calls]

    inbound = names.index("_apply_visibility_inbound_middleware")
    read_only = names.index("_apply_read_only_middleware")
    policy = names.index("_apply_tool_security_policies")
    outbound = names.index("_apply_visibility_outbound_middleware")
    assert inbound < read_only < policy < outbound

    # The outbound half must stay innermost: nothing that can register
    # middleware may run after it.
    after = names[outbound + 1 :]
    assert not [
        n for n in after if n.startswith("_apply_") or n == "mcp.add_middleware"
    ], f"middleware-installing calls after the outbound half: {after}"
