"""Visibility enforce-mode middleware wiring (#2015).

FastMCP registration order IS the inbound execution order (first added =
outermost), and the enforcement pair's placement is load-bearing: the inbound
conceal/refuse gate must precede the read-only guard and PolicyMiddleware so a
hidden entity_id never reaches the approval queue (or earns an
approval-pending response that confirms existence), and the outbound scan must
be registered last (innermost) so it sees raw tool output.

The chain read here is the one the real server registers at construction,
with every optional middleware (security policies, request queue) switched on
so none of them can slip in behind the outbound scan unnoticed.
"""

from __future__ import annotations

import pytest

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.ha_request_queue import HomeAssistantRequestQueueMiddleware
from ha_mcp.policy.middleware import PolicyMiddleware
from ha_mcp.read_only import ReadOnlyMiddleware
from ha_mcp.visibility.enforcement import (
    VisibilityInboundEnforcement,
    VisibilityOutboundEnforcement,
)

from ._real_server import build_server


@pytest.fixture
def middleware(
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> list[object]:
    """The registered middleware chain, outermost first, with all options on."""
    monkeypatch.setenv("ENABLE_TOOL_SECURITY_POLICIES", "true")
    monkeypatch.setenv("HA_TOOL_CONCURRENCY", "2")
    return list(build_server(monkeypatch, core_client).mcp.middleware)


def _positions(middleware: list[object], kind: type) -> list[int]:
    return [index for index, item in enumerate(middleware) if isinstance(item, kind)]


@pytest.mark.parametrize(
    "kind", [VisibilityInboundEnforcement, VisibilityOutboundEnforcement]
)
def test_each_enforcement_half_is_installed_once(
    middleware: list[object], kind: type
) -> None:
    """Enforcement is always installed: it consults the live config per call."""
    assert len(_positions(middleware, kind)) == 1


def test_inbound_gate_precedes_read_only_and_policy_and_outbound_is_innermost(
    middleware: list[object],
) -> None:
    """A hidden call is concealed before any other gate answers it."""
    (inbound,) = _positions(middleware, VisibilityInboundEnforcement)
    (read_only,) = _positions(middleware, ReadOnlyMiddleware)
    (policy,) = _positions(middleware, PolicyMiddleware)
    (queue,) = _positions(middleware, HomeAssistantRequestQueueMiddleware)
    (outbound,) = _positions(middleware, VisibilityOutboundEnforcement)
    assert inbound < read_only < policy < outbound
    assert queue < outbound

    # Nothing may run after the outbound half, so its scan sees raw output.
    assert outbound == len(middleware) - 1, (
        f"middleware after the outbound half: {middleware[outbound + 1 :]}"
    )
